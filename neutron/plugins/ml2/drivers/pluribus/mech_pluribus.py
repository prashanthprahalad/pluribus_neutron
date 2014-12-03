#
# COPYRIGHT 2014 Pluribus Networks Inc.
#
# All rights reserved. This copyright notice is Copyright Management
# Information under 17 USC 1202 and is included to protect this work and
# deter copyright infringement.  Removal or alteration of this Copyright
# Management Information without the express written permission from
# Pluribus Networks Inc is prohibited, and any such unauthorized removal
# or alteration will be a violation of federal law.
#

from oslo.config import cfg

from neutron import manager
from neutron.api.v2 import attributes
from neutron.common import constants as const
from neutron.extensions import portbindings
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.plugins.ml2 import driver_api
from neutron.plugins.ml2.common import exceptions as ml2_exc
from neutron.plugins.ml2.drivers.mech_pluribus import config  # noqa
from oslo.utils import importutils

LOG = logging.getLogger(__name__)


class PluribusDriver(driver_api.MechanismDriver):

    """Ml2 Mechanism Driver for the Pluribus Networks hardware.
    """

    def initialize(self):

        # setup the rpc server
        self.server = importutils.\
            import_object(cfg.CONF.PLURIBUS_PLUGINS['pn_api'])
        self.vif_type = portbindings.VIF_TYPE_OVS
        self.vif_details = {portbindings.CAP_PORT_FILTER: False}

        LOG.debug("%(module)s.%(name)s init done",
                  {'module': __name__,
                   'name': self.__class__.__name__})

    @property
    def core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def create_network_postcommit(self, context):
        LOG.debug(('Pluribus Driver create_network_postcommit() called:',
                   context.current))
        network = context.current
        network['router_external'] = network.pop('router:external')
        self.server.create_network(**network)
        LOG.info(_LI("Pluribus successfully created network",
                 network['name']))

    def update_network_postcommit(self, context):
        LOG.debug(('update network operation is not supported by Pluribus'))
        raise ml2_exc.MechanismDriverError(method='update_network_postcommit')

    def delete_network_postcommit(self, context):
        LOG.debug(('Pluribus delete_network_postcommit() called:',
                   context.current))
        network = context.current
        network['router_external'] = network.pop('router:external')
        self.server.delete_network(**network)
        LOG.info(_LI("Pluribus successfully deleted network",
                 network['name']))

    def create_port_postcommit(self, context):
        LOG.debug(('Pluribus create_port_postcommit() called:',
                   context.current))
        port = context.current
        # port names ending with '-dhcp' are specially used by Pluribus
        if port['name'].endswith('-dhcp'):
            raise ml2_exc.MechanismDriverError(method='create_port_postcommit')

        self.server.create_port(**port)
        LOG.info(_LI("Pluribus successfully created port", port['name']))

    def bind_port(self, context):
        LOG.debug("Attempting to bind port %(port)s on network %(network)s",
                  {'port': context.current['id'],
                   'network': context.network.current['id']})
        for segment in context.network.network_segments:
            context.set_binding(segment[driver_api.ID],
                                self.vif_type, self.vif_details,
                                status=const.PORT_STATUS_ACTIVE)
            LOG.debug("Bound using segment: %s", segment)

    def update_port_postcommit(self, context):
        LOG.debug(('Pluribus update_port_postcommit() called:',
                   context.current))
        port = context.current
        self.server.update_port(**port)
        LOG.info(_LI("Pluribus successfully updated port",
                 port['name']))

    def delete_port_postcommit(self, context):
        LOG.debug(('Pluribus delete_port_postcommit() called:',
                   context.current))
        port = context.current
        # get the service plugin to disassociate floating ip
        service_plugins = manager.NeutronManager.get_service_plugins()
        l3_plugin = service_plugins.get(constants.L3_ROUTER_NAT)
        l3_plugin.disassociate_floatingips(context._plugin_context, port['id'])
        LOG.info(_LI("Pluribus successfully deleted port",
                 port['name']))
        self.server.delete_port(**port)

    def create_subnet_postcommit(self, context):
        LOG.debug(('Pluribus create_subnet_postcommit() called:',
                   context.current))
        subnet = context.current

        # get the network information
        net = self.core_plugin.get_network(context._plugin_context,
                                           subnet['network_id'])

        # create a new port for dhcp endpoint on the switch if it is not marked
        # as 'external network'.
        # for an 'external network' we use the gateway ip information which is
        # provided by admin during network create - the assumption is that the
        # gateway IP is within the openstack infrastructure and not outside it
        if net.get('router:external', None) and \
                subnet['gateway_ip'] is not None:

            dhcp_ip = subnet['gateway_ip']
        else:
            fixed_ip = {'subnet_id': subnet['id']}
            port_name = subnet['name'] + '-dhcp'
            port_data = {
                'tenant_id': subnet['tenant_id'],
                'name': port_name,
                'network_id': subnet['network_id'],
                'mac_address': attributes.ATTR_NOT_SPECIFIED,
                'admin_state_up': True,
                'device_id': '',
                'device_owner': const.DEVICE_OWNER_DHCP,
                'fixed_ips': [fixed_ip]
            }

            # create a port for dhcp
            dhcp_port = self.core_plugin.create_port(context._plugin_context,
                                                     {'port': port_data})
            dhcp_ip = dhcp_port['fixed_ips'][0]['ip_address']

        LOG.debug(('dhcp_ip == ', dhcp_ip))
        subnet['dhcp_ip'] = dhcp_ip

        try:
            self.server.create_subnet(**subnet)
            LOG.info(_LI("Pluribus successfully created subnet",
                     subnet['name']))
        except Exception as e:
            LOG.error(_LE('create_subnet failed, rolling back'))
            # delete the dhcp port if enable dhcp was set
            if subnet['enable_dhcp']:
                self.core_plugin.delete_port(context._plugin_context,
                                             dhcp_port['id'], False)
            raise e

        if not net['router:external']:
            # set the DHCP port status to active
            filters = {'subnet_id': [subnet['id']]}
            dhcp_ports = self.core_plugin.get_ports(context._plugin_context,
                                                    filters=filters)
            for port in dhcp_ports:
                port['status'] = const.PORT_STATUS_ACTIVE
                self.core_plugin.update_port(context._plugin_context,
                                             port['id'], {'port': port})

    def update_subnet_postcommit(self, context):
        LOG.debug(('update subnet operation not supported by Pluribus'))
        raise ml2_exc.MechanismDriverError(method='update_subnet_postcommit')

    def delete_subnet_postcommit(self, context):
        LOG.debug(('Pluribus delete_subnet_postcommit() called:',
                   context.current))
        subnet = context.current
        self.server.delete_subnet(**subnet)
        LOG.info(_LI("Pluribus successfully deleted subnet",
                 subnet['name']))
