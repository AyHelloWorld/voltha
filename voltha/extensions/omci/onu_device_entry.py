#
# Copyright 2018 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import structlog
from voltha.extensions.omci.omci_defs import EntityOperations, ReasonCodes
import voltha.extensions.omci.omci_entities as omci_entities
from voltha.extensions.omci.omci_cc import OMCI_CC
from common.event_bus import EventBusClient
from voltha.extensions.omci.tasks.task_runner import TaskRunner
from voltha.extensions.omci.onu_configuration import OnuConfiguration
from voltha.extensions.omci.tasks.reboot_task import OmciRebootRequest, RebootFlags
from voltha.extensions.omci.tasks.omci_modify_request import OmciModifyRequest
from voltha.extensions.omci.omci_me import OntGFrame

from twisted.internet import reactor
from enum import IntEnum

OP = EntityOperations
RC = ReasonCodes

ACTIVE_KEY = 'active'
IN_SYNC_KEY = 'in-sync'
LAST_IN_SYNC_KEY = 'last-in-sync-time'
SUPPORTED_MESSAGE_ENTITY_KEY = 'managed-entities'
SUPPORTED_MESSAGE_TYPES_KEY = 'message-type'


class OnuDeviceEvents(IntEnum):
    # Events of interest to Device Adapters and OpenOMCI State Machines
    DeviceStatusEvent = 0       # OnuDeviceEntry running status changed
    MibDatabaseSyncEvent = 1    # MIB database sync changed
    OmciCapabilitiesEvent = 2   # OMCI ME and message type capabilities
    AlarmDatabaseSyncEvent = 3  # Alarm database sync changed

    # TODO: Add other events here as needed


class OnuDeviceEntry(object):
    """
    An ONU Device entry in the MIB
    """
    def __init__(self, omci_agent, device_id, adapter_agent, custom_me_map,
                 mib_db, support_classes):
        """
        Class initializer

        :param omci_agent: (OpenOMCIAgent) Reference to OpenOMCI Agent
        :param device_id: (str) ONU Device ID
        :param adapter_agent: (AdapterAgent) Adapter agent for ONU
        :param custom_me_map: (dict) Additional/updated ME to add to class map
        :param mib_db: (MibDbApi) MIB Database reference
        :param support_classes: (dict) State machines and tasks for this ONU
        """
        self.log = structlog.get_logger(device_id=device_id)

        self._started = False
        self._omci_agent = omci_agent         # OMCI AdapterAgent
        self._device_id = device_id           # ONU Device ID
        self._runner = TaskRunner(device_id)  # OMCI_CC Task runner
        self._deferred = None
        self._first_in_sync = False

        # OMCI related databases are on a per-agent basis. State machines and tasks
        # are per ONU Vendor
        #
        self._support_classes = support_classes
        self._configuration = None

        try:
            # MIB Synchronization state machine
            self._mib_db_in_sync = False
            mib_synchronizer_info = support_classes.get('mib-synchronizer')
            advertise = mib_synchronizer_info['advertise-events']
            self._mib_sync_sm = mib_synchronizer_info['state-machine'](self._omci_agent,
                                                                       device_id,
                                                                       mib_synchronizer_info['tasks'],
                                                                       mib_db,
                                                                       advertise_events=advertise)
            # ONU OMCI Capabilities state machine
            capabilities_info = support_classes.get('omci-capabilities')
            advertise = capabilities_info['advertise-events']
            self._capabilities_sm = capabilities_info['state-machine'](self._omci_agent,
                                                                       device_id,
                                                                       capabilities_info['tasks'],
                                                                       advertise_events=advertise)
            # ONU Performance Monitoring Intervals state machine
            interval_info = support_classes.get('performance-intervals')
            advertise = interval_info['advertise-events']
            self._pm_intervals_sm = interval_info['state-machine'](self._omci_agent, device_id,
                                                                   interval_info['tasks'],
                                                                   advertise_events=advertise)

            # ONU ALARM Synchronization state machine
            self._alarm_db_in_sync = False
            alarm_synchronizer_info = support_classes.get('alarm-syncronizer')
            advertise = alarm_synchronizer_info['advertise-events']
            self._alarm_sync_sm = alarm_synchronizer_info['state-machine'](self._omci_agent,
                                                                           device_id,
                                                                           alarm_synchronizer_info['tasks'],
                                                                           mib_db,
                                                                           advertise_events=advertise)
        except Exception as e:
            self.log.exception('state-machine-create-failed', e=e)
            raise

        # Put state machines in the order you wish to start them

        self._state_machines = []
        self._on_start_state_machines = [       # Run when 'start()' called
            self._mib_sync_sm,
            self._capabilities_sm,
            self._alarm_sync_sm,
        ]
        self._on_sync_state_machines = [        # Run after first in_sync event
            self._pm_intervals_sm
        ]
        self._custom_me_map = custom_me_map
        self._me_map = omci_entities.entity_id_to_class_map.copy()

        if custom_me_map is not None:
            self._me_map.update(custom_me_map)

        self.event_bus = EventBusClient()

        # Create OMCI communications channel
        self._omci_cc = OMCI_CC(adapter_agent, self.device_id, self._me_map)

    @staticmethod
    def event_bus_topic(device_id, event):
        """
        Get the topic name for a given event for this ONU Device
        :param device_id: (str) ONU Device ID
        :param event: (OnuDeviceEvents) Type of event
        :return: (str) Topic string
        """
        assert event in OnuDeviceEvents, \
            'Event {} is not an ONU Device Event'.format(event.name)
        return 'omci-device:{}:{}'.format(device_id, event.name)

    @property
    def device_id(self):
        return self._device_id

    @property
    def omci_cc(self):
        return self._omci_cc

    @property
    def task_runner(self):
        return self._runner

    @property
    def mib_synchronizer(self):
        """
        Reference to the OpenOMCI MIB Synchronization state machine for this ONU
        """
        return self._mib_sync_sm

    @property
    def omci_capabilities(self):
        """
        Reference to the OpenOMCI OMCI Capabilities state machine for this ONU
        """
        return self._capabilities_sm

    @property
    def pm_intervals_state_machine(self):
        """
        Reference to the OpenOMCI PM Intervals state machine for this ONU
        """
        return self._pm_intervals_sm

    @property
    def alarm_synchronizer(self):
        """
        Reference to the OpenOMCI Alarm Synchronization state machine for this ONU
        """
        return self._alarm_sync_sm

    @property
    def active(self):
        """
        Is the ONU device currently active/running
        """
        return self._started

    @property
    def custom_me_map(self):
        """ Vendor-specific Managed Entity Map for this vendor's device"""
        return self._custom_me_map

    @property
    def me_map(self):
        """ Combined ME and Vendor-specific Managed Entity Map for this device"""
        return self._me_map

    def _cancel_deferred(self):
        d, self._deferred = self._deferred, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass

    @property
    def mib_db_in_sync(self):
        return self._mib_db_in_sync

    @mib_db_in_sync.setter
    def mib_db_in_sync(self, value):
        if self._mib_db_in_sync != value:
            # Save value
            self._mib_db_in_sync = value

            # Start up other state machines if needed
            if self._first_in_sync:
                self.first_in_sync_event()

            # Notify any event listeners
            topic = OnuDeviceEntry.event_bus_topic(self.device_id,
                                                   OnuDeviceEvents.MibDatabaseSyncEvent)
            msg = {
                IN_SYNC_KEY: self._mib_db_in_sync,
                LAST_IN_SYNC_KEY: self.mib_synchronizer.last_mib_db_sync
            }
            self.event_bus.publish(topic=topic, msg=msg)

    @property
    def alarm_db_in_sync(self):
        return self._alarm_db_in_sync

    @alarm_db_in_sync.setter
    def alarm_db_in_sync(self, value):
        if self._alarm_db_in_sync != value:
            # Save value
            self._alarm_db_in_sync = value

            # Start up other state machines if needed
            if self._first_in_sync:
                self.first_in_sync_event()

            # Notify any event listeners
            topic = OnuDeviceEntry.event_bus_topic(self.device_id,
                                                   OnuDeviceEvents.AlarmDatabaseSyncEvent)
            msg = {
                IN_SYNC_KEY: self._alarm_db_in_sync,
                LAST_IN_SYNC_KEY: self.alarm_synchronizer.last_alarm_sync_time
            }
            self.event_bus.publish(topic=topic, msg=msg)

    @property
    def configuration(self):
        """
        Get the OMCI Configuration object for this ONU.  This is a class that provides some
        common database access functions for ONU capabilities and read-only configuration values.

        :return: (OnuConfiguration)
        """
        return self._configuration

    def start(self):
        """
        Start the ONU Device Entry state machines
        """
        if self._started:
            return

        self._started = True
        self._omci_cc.enabled = True
        self._first_in_sync = True
        self._runner.start()
        self._configuration = OnuConfiguration(self._omci_agent, self._device_id)

        # Start MIB Sync and other state machines that can run before the first
        # MIB Synchronization event occurs. Start 'later' so that any
        # ONU Device, OMCI DB, OMCI Agent, and others are fully started before
        # performing the start.

        self._state_machines = []

        def start_state_machines(machines):
            for sm in machines:
                self._state_machines.append(sm)
                sm.start()

        self._deferred = reactor.callLater(0, start_state_machines,
                                           self._on_start_state_machines)
        # Notify any event listeners
        self._publish_device_status_event()

    def stop(self):
        """
        Stop the ONU Device Entry state machines
        """
        if not self._started:
            return

        self._started = False
        self._cancel_deferred()
        self._omci_cc.enabled = False

        # Halt MIB Sync and other state machines
        for sm in self._state_machines:
            sm.stop()

        self._state_machines = []

        # Stop task runner
        self._runner.stop()

        # Notify any event listeners
        self._publish_device_status_event()

    def first_in_sync_event(self):
        """
        This event is called on the first MIB synchronization event after
        OpenOMCI has been started. It is responsible for starting any
        other state machine and to initiate an ONU Capabilities report
        """
        if self._first_in_sync:
            self._first_in_sync = False

            # Start up the ONU Capabilities task
            self._configuration.reset()

            # Insure that the ONU-G Administrative lock is disabled
            def failure(reason):
                self.log.error('disable-admin-state-lock', reason=reason)

            frame = OntGFrame(attributes={'administrative_state': 0}).set()
            task = OmciModifyRequest(self._omci_agent, self.device_id, frame)
            self.task_runner.queue_task(task).addErrback(failure)

            # Start up any other remaining OpenOMCI state machines
            def start_state_machines(machines):
                for sm in machines:
                    self._state_machines.append(sm)
                    reactor.callLater(0, sm.start)

            self._deferred = reactor.callLater(0, start_state_machines,
                                               self._on_sync_state_machines)

    def _publish_device_status_event(self):
        """
        Publish the ONU Device start/start status.
        """
        topic = OnuDeviceEntry.event_bus_topic(self.device_id,
                                               OnuDeviceEvents.DeviceStatusEvent)
        msg = {ACTIVE_KEY: self._started}
        self.event_bus.publish(topic=topic, msg=msg)

    def publish_omci_capabilities_event(self):
        """
        Publish the ONU Device start/start status.
        """
        topic = OnuDeviceEntry.event_bus_topic(self.device_id,
                                               OnuDeviceEvents.OmciCapabilitiesEvent)
        msg = {
            SUPPORTED_MESSAGE_ENTITY_KEY: self.omci_capabilities.supported_managed_entities,
            SUPPORTED_MESSAGE_TYPES_KEY: self.omci_capabilities.supported_message_types
        }
        self.event_bus.publish(topic=topic, msg=msg)

    def delete(self):
        """
        Stop the ONU Device's state machine and remove the ONU, and any related
        OMCI state information from the OpenOMCI Framework
        """
        self.stop()
        self.mib_synchronizer.delete()

        # OpenOMCI cleanup
        if self._omci_agent is not None:
            self._omci_agent.remove_device(self._device_id, cleanup=True)

    def query_mib(self, class_id=None, instance_id=None, attributes=None):
        """
        Get MIB database information.

        This method can be used to request information from the database to the detailed
        level requested

        :param class_id:  (int) Managed Entity class ID
        :param instance_id: (int) Managed Entity instance
        :param attributes: (list or str) Managed Entity instance's attributes

        :return: (dict) The value(s) requested. If class/inst/attribute is
                        not found, an empty dictionary is returned
        :raises DatabaseStateError: If the database is not enabled
        """
        self.log.debug('query', class_id=class_id, instance_id=instance_id,
                       attributes=attributes)

        return self.mib_synchronizer.query_mib(class_id=class_id, instance_id=instance_id,
                                               attributes=attributes)

    def query_mib_single_attribute(self, class_id, instance_id, attribute):
        """
        Get MIB database information for a single specific attribute

        This method can be used to request information from the database to the detailed
        level requested

        :param class_id:  (int) Managed Entity class ID
        :param instance_id: (int) Managed Entity instance
        :param attribute: (str) Managed Entity instance's attribute

        :return: (varies) The value requested. If class/inst/attribute is
                          not found, None is returned
        :raises DatabaseStateError: If the database is not enabled
        """
        self.log.debug('query-single', class_id=class_id,
                       instance_id=instance_id, attributes=attribute)
        assert isinstance(attribute, basestring), \
            'Only a single attribute value can be retrieved'

        entry = self.mib_synchronizer.query_mib(class_id=class_id,
                                                instance_id=instance_id,
                                                attributes=attribute)

        return entry[attribute] if attribute in entry else None

    def reboot(self,
               flags=RebootFlags.Reboot_Unconditionally,
               timeout=OmciRebootRequest.DEFAULT_PRIORITY):
        """
        Request a reboot of the ONU

        :param flags: (RebootFlags) Reboot condition
        :param timeout: (int) Reboot task priority
        :return: (deferred) Fires upon completion or error
        """
        assert self.active, 'This device is not active'

        return self.task_runner.queue_task(OmciRebootRequest(self._omci_agent,
                                                             self.device_id,
                                                             flags=flags,
                                                             timeout=timeout))
