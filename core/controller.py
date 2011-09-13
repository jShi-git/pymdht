# Copyright (C) 2009-2010 Raul Jimenez
# Released under GNU LGPL 2.1
# See LICENSE.txt for more information

"""
The controller module is designed to be the central point where most modules
are connected. This module delegates most of the implementation details to
other modules. This delegation model creates separated responsibility areas
where implementation can be changed in isolation.

The extreme cases are the plug-ins which allow us to develop/run different
implementations of routing and lookup managers in parallel.

"""

size_estimation = False

import sys
import ptime as time
import os
import cPickle

import logging, logging_conf

import state
import identifier
from identifier import Id
import message
import token_manager
import tracker
from querier import Querier
from message import QUERY, RESPONSE, ERROR
from node import Node
import pkgutil

#from profilestats import profile

logger = logging.getLogger('dht')

SAVE_STATE_DELAY = 1 * 60
STATE_FILENAME = 'pymdht.state'

#TIMEOUT_DELAY = 2

NUM_NODES = 8


class Controller:

    def __init__(self, pymdht_version,
                 my_node, state_filename,
                 routing_m_mod, lookup_m_mod,
                 experimental_m_mod,
                 private_dht_name):
        self.num_p_in = 0
        self.num_fn_in = 0
        self.num_gp_in = 0
        self.num_ap_in = 0
        self.num_r_in = 0
        self.num_p_out = 0
        self.num_fn_out = 0
        self.num_gp_out = 0
        self.num_ap_out = 0
        self.num_r_out = 0
        self.num_ignored_fn = 0
        self.last_print_ts = 0
        self.responded_fn = {}
        self.last_responded_fn_cleanup = 0

        if size_estimation:
            self._size_estimation_file = open('size_estimation.dat', 'w')
        
        
        self.state_filename = state_filename
        saved_id, saved_bootstrap_nodes = state.load(self.state_filename)
        my_addr = my_node.addr
        self._my_id = my_node.id # id indicated by user 
        if not self._my_id:
            self._my_id = saved_id # id loaded from file
        if not self._my_id:
            self._my_id = self._my_id = identifier.RandomId() # random id
        self._my_node = Node(my_addr, self._my_id)
        self.msg_f = message.MsgFactory(pymdht_version, self._my_id,
                                        private_dht_name)
        self._tracker = tracker.Tracker()
        self._token_m = token_manager.TokenManager()

        self._querier = Querier()
        self._routing_m = routing_m_mod.RoutingManager(
            self._my_node, saved_bootstrap_nodes, self.msg_f)
        self._lookup_m = lookup_m_mod.LookupManager(self._my_id, self.msg_f)
        self._experimental_m = experimental_m_mod.ExperimentalManager(
            self._my_node.id, self.msg_f) 
                  
        current_ts = time.time()
        self._next_save_state_ts = current_ts + SAVE_STATE_DELAY
        self._next_maintenance_ts = current_ts
        self._next_timeout_ts = current_ts
        self._next_main_loop_call_ts = current_ts
        self._pending_lookups = []
                
    def on_stop(self):
        self._experimental_m.on_stop()

    def get_peers(self, lookup_id, info_hash, callback_f, bt_port=0):
        """
        Start a get\_peers lookup whose target is 'info\_hash'. The handler
        'callback\_f' will be called with two arguments ('lookup\_id' and a
        'peer list') whenever peers are discovered. Once the lookup is
        completed, the handler will be called with 'lookup\_id' and None as
        arguments.

        This method is designed to be used as minitwisted's external handler.

        """
        logger.debug('get_peers %d %r' % (bt_port, info_hash))
        self._pending_lookups.append(self._lookup_m.get_peers(lookup_id,
                                                              info_hash,
                                                              callback_f,
                                                              bt_port))
        queries_to_send =  self._try_do_lookup()
        datagrams_to_send = self._register_queries(queries_to_send)
        return datagrams_to_send
    
    def _try_do_lookup(self):
        queries_to_send = []
        if self._pending_lookups:
            lookup_obj = self._pending_lookups[0]
        else:
            return queries_to_send
        log_distance = lookup_obj.info_hash.log_distance(self._my_id)
        bootstrap_rnodes = self._routing_m.get_closest_rnodes(log_distance,
                                                              0,
                                                              True)
        #TODO: get the full bucket
        if bootstrap_rnodes:
            del self._pending_lookups[0]
            # look if I'm tracking this info_hash
            peers = self._tracker.get(lookup_obj.info_hash)
            callback_f = lookup_obj.callback_f
            if peers and callback_f and callable(callback_f):
                callback_f(lookup_obj.lookup_id, peers, None)
            # do the lookup
            queries_to_send = lookup_obj.start(bootstrap_rnodes)
        else:
            next_lookup_attempt_ts = time.time() + .2
            self._next_main_loop_call_ts = min(self._next_main_loop_call_ts,
                                               next_lookup_attempt_ts)
        return queries_to_send
    
    def print_routing_table_stats(self):
        self._routing_m.print_stats()

    def main_loop(self):
        """
        Perform maintenance operations. The main operation is routing table
        maintenance where staled nodes are added/probed/replaced/removed as
        needed. The routing management module specifies the implementation
        details.  This includes keeping track of queries that have not been
        responded for a long time (timeout) with the help of
        querier.Querier. The routing manager and the lookup manager will be
        informed of those timeouts.

        This method is designed to be used as minitwisted's heartbeat handler.

        """

        queries_to_send = []
        current_ts = time.time()
        #TODO: I think this if should be removed
        # At most, 1 second between calls to main_loop after the first call
        if current_ts >= self._next_main_loop_call_ts:
            self._next_main_loop_call_ts = current_ts + 1
        else:
            # It's too early
            return self._next_main_loop_call_ts, []
        # Retry failed lookup (if any)
        queries_to_send.extend(self._try_do_lookup())
        
        # Take care of timeouts
        if current_ts >= self._next_timeout_ts:
            (self._next_timeout_ts,
             timeout_queries) = self._querier.get_timeout_queries()
            for query in timeout_queries:
                queries_to_send.extend(self._on_timeout(query))

        # Routing table maintenance
        if time.time() >= self._next_maintenance_ts:
            (maintenance_delay,
             queries,
             maintenance_lookup) = self._routing_m.do_maintenance()
            self._next_maintenance_ts = current_ts + maintenance_delay
            self._next_main_loop_call_ts = min(self._next_main_loop_call_ts,
                                               self._next_maintenance_ts)
            queries_to_send.extend(queries)
            if maintenance_lookup:
                target, rnodes = maintenance_lookup
                lookup_obj = self._lookup_m.maintenance_lookup(target)
                queries_to_send.extend(lookup_obj.start(rnodes))
            
        # Auto-save routing table
        if current_ts >= self._next_save_state_ts:
            state.save(self._my_id,
                       self._routing_m.get_main_rnodes(),
                       self.state_filename)
            self._next_save_state_ts = current_ts + SAVE_STATE_DELAY
            self._next_main_loop_call_ts = min(self._next_main_loop_call_ts,
                                               self._next_maintenance_ts,
                                               self._next_timeout_ts,
                                               self._next_save_state_ts)
        # Return control to reactor
        datagrams_to_send = self._register_queries(queries_to_send)
        return self._next_main_loop_call_ts, datagrams_to_send

    def _maintenance_lookup(self, target):
        self._lookup_m.maintenance_lookup(target)

    def on_datagram_received(self, datagram):
        """
        Perform the actions associated to the arrival of the given
        datagram. The datagram will be ignored in cases such as invalid
        format. Otherwise, the datagram will be decoded and different modules
        will be informed to take action on it. For instance, if the datagram
        contains a response to a lookup query, both routing and lookup manager
        will be informed. Additionally, if that response contains peers, the
        lookup's handler will be called (see get\_peers above).
        This method is designed to be used as minitwisted's networking handler.

        """
        current_time = time.time()
        current_time_int = int(current_time)
        if current_time_int > self.last_print_ts:
            logger.critical(
                "IN: %d p, %d fn, %d gp, %d ap, %d r\nOUT: %d p, %d fn, %d gp, %d ap, %d r\n%d" % (
                    self.num_p_in, self.num_fn_in, self.num_gp_in, self.num_ap_in, self.num_r_in,
                    self.num_p_out, self.num_fn_out, self.num_gp_out, self.num_ap_out, self.num_r_out,
                    self.num_ignored_fn))
            self.num_p_in = self.num_fn_in = self.num_gp_in = self.num_ap_in = self.num_r_in = 0
            self.num_p_out = self.num_fn_out = self.num_gp_out = self.num_ap_out = self.num_r_out = 0
            self.num_ignored_fn = 0
            self.last_print_ts = current_time_int

        exp_queries_to_send = []
        
        data = datagram.data
        addr = datagram.addr
        datagrams_to_send = []
        try:
            msg = self.msg_f.incoming_msg(datagram)
            
        except(message.MsgError):
            # ignore message
            return self._next_main_loop_call_ts, datagrams_to_send

        if msg.type == message.QUERY:
            if msg.query == message.PING:
                self.num_p_in += 1
                return self._next_main_loop_call_ts, datagrams_to_send
            elif msg.query == message.FIND_NODE:
                self.num_fn_in += 1
                if time.time() > self.last_responded_fn_cleanup + 3600:
                    self.responded_fn = {}
                    self.last_responded_fn_cleanup = time.time()
                ip = datagram.addr[0]
                self.responded_fn[ip] = self.responded_fn.get(ip, 0) + 1
                if self.responded_fn[ip] > 2:
                    self.num_ignored_fn += 1
                    return self._next_main_loop_call_ts, datagrams_to_send
            if msg.query == message.GET_PEERS:
                self.num_gp_in += 1
            if msg.query == message.ANNOUNCE_PEER:
                self.num_ap_in += 1

            if msg.src_id == self._my_id:
                logger.debug('Got a msg from myself:\n%r', msg)
                return self._next_main_loop_call_ts, datagrams_to_send
            #zinat: inform experimental_module
            exp_queries_to_send = self._experimental_m.on_query_received(msg)
            
            response_msg = self._get_response(msg)
            if response_msg:
                bencoded_response = response_msg.stamp(msg.tid)
                datagrams_to_send.append(
                    message.Datagram(bencoded_response, addr))
            maintenance_queries_to_send = self._routing_m.on_query_received(
                msg.src_node)
            
        elif msg.type == message.RESPONSE:
            self.num_r_in += 1
            related_query = self._querier.get_related_query(msg)
            if not related_query:
                # Query timed out or unrequested response
                return self._next_main_loop_call_ts, datagrams_to_send
            ## zinat: if related_query.experimental_obj:
            exp_queries_to_send = self._experimental_m.on_response_received(
                                                        msg, related_query)
            #TODO: you need to get datagrams to be able to send messages (raul)
            # lookup related tasks
            if related_query.lookup_obj:
                (lookup_queries_to_send,
                 peers,
                 num_parallel_queries,
                 lookup_done
                 ) = related_query.lookup_obj.on_response_received(
                    msg, msg.src_node)
                datagrams = self._register_queries(lookup_queries_to_send)
                datagrams_to_send.extend(datagrams)

                lookup_id = related_query.lookup_obj.lookup_id
                callback_f = related_query.lookup_obj.callback_f
                if peers and callable(callback_f):
                    callback_f(lookup_id, peers, msg.src_node)
                if lookup_done:
                    if callable(callback_f):
                        callback_f(lookup_id, None, msg.src_node)
                    queries_to_send = self._announce(
                        related_query.lookup_obj)
                    datagrams = self._register_queries(
                        queries_to_send)
                    datagrams_to_send.extend(datagrams)
                        
                # Size estimation
                if size_estimation and lookup_done:
                    line = '%d %d\n' % (
                        related_query.lookup_obj.get_number_nodes_within_region())
                    self._size_estimation_file.write(line)
                    self._size_estimation_file.flush()
                    
            # maintenance related tasks
            maintenance_queries_to_send = \
                self._routing_m.on_response_received(
                msg.src_node, related_query.rtt, msg.all_nodes)

        elif msg.type == message.ERROR:
            related_query = self._querier.get_related_query(msg)
            if not related_query:
                # Query timed out or unrequested response
                return self._next_main_loop_call_ts, datagrams_to_send
            #TODO: zinat: same as response
            exp_queries_to_send = self._experimental_m.on_error_received(msg, related_query)
            # lookup related tasks
            if related_query.lookup_obj:
                peers = None # an error msg doesn't have peers
                (lookup_queries_to_send,
                 num_parallel_queries,
                 lookup_done
                 ) = related_query.lookup_obj.on_error_received(msg)
                datagrams = self._register_queries(lookup_queries_to_send)
                datagrams_to_send.extend(datagrams)

                if lookup_done:
                    # Size estimation
                    if size_estimation:
                        line = '%d %d\n' % (
                            related_query.lookup_obj.get_number_nodes_within_region())
                        self._size_estimation_file.write(line)
                        self._size_estimation_file.flush()




                    
                    datagrams = self._announce(related_query.lookup_obj)
                    datagrams_to_send.extend(datagrams)
                callback_f = related_query.lookup_obj.callback_f
                if callback_f and callable(callback_f):
                    lookup_id = related_query.lookup_obj.lookup_id
                    if lookup_done:
                        callback_f(lookup_id, None, msg.src_node)
			    # maintenance related tasks
            maintenance_queries_to_send = \
                self._routing_m.on_error_received(addr)

        else: # unknown type
            return self._next_main_loop_call_ts, datagrams_to_send
        # we are done with the plugins
        # now we have maintenance_queries_to_send, let's send them!
        datagrams = self._register_queries(maintenance_queries_to_send)
        datagrams_to_send.extend(datagrams)
        if exp_queries_to_send:
            datagrams = self._register_queries(exp_queries_to_send)
            datagrams_to_send.extend(datagrams)
        return self._next_main_loop_call_ts, datagrams_to_send

    def _on_query_received(self):
        return
    def _on_response_received(self):
        return
    def _on_error_received(self):
        return
    
    
    def _get_response(self, msg):
        if msg.query == message.PING:
            return self.msg_f.outgoing_ping_response(msg.src_node)
        elif msg.query == message.FIND_NODE:
            log_distance = msg.target.log_distance(self._my_id)
            rnodes = self._routing_m.get_closest_rnodes(log_distance,
                                                        NUM_NODES, False)
            #TODO: return the closest rnodes to the target instead of the 8
            #first in the bucket.
            return self.msg_f.outgoing_find_node_response(
                msg.src_node, rnodes)
        elif msg.query == message.GET_PEERS:
            token = self._token_m.get()
            log_distance = msg.info_hash.log_distance(self._my_id)
            rnodes = self._routing_m.get_closest_rnodes(log_distance,
                                                        NUM_NODES, False)
            #TODO: return the closest rnodes to the target instead of the 8
            #first in the bucket.
            peers = self._tracker.get(msg.info_hash)
            if peers:
                logger.debug('RESPONDING with PEERS:\n%r' % peers)
            return self.msg_f.outgoing_get_peers_response(
                msg.src_node, token, nodes=rnodes, peers=peers)
        elif msg.query == message.ANNOUNCE_PEER:
            peer_addr = (msg.src_addr[0], msg.bt_port)
            self._tracker.put(msg.info_hash, peer_addr)
            return self.msg_f.outgoing_announce_peer_response(msg.src_node)
        else:
            logger.debug('Invalid QUERY: %r' % (msg.query))
            #TODO: maybe send an error back?
        
    def _on_timeout(self, related_query):
        queries_to_send = []
        #TODO: on_timeout should return queries (raul)
        exp_queries_to_send = self._experimental_m.on_timeout(related_query)
        if related_query.lookup_obj:
            (lookup_queries_to_send,
             num_parallel_queries,
             lookup_done
             ) = related_query.lookup_obj.on_timeout(related_query.dst_node)
            queries_to_send.extend(lookup_queries_to_send)
            callback_f = related_query.lookup_obj.callback_f
            if lookup_done:
                # Size estimation
                if size_estimation:
                    line = '%d %d\n' % (
                        related_query.lookup_obj.get_number_nodes_within_region())
                    self._size_estimation_file.write(line)
                    self._size_estimation_file.flush()


                if callback_f and callable(callback_f):
                    queries_to_send.extend(self._announce(
                            related_query.lookup_obj))
                    lookup_id = related_query.lookup_obj.lookup_id
                    related_query.lookup_obj.callback_f(lookup_id, None, None)
        maintenance_queries_to_send = self._routing_m.on_timeout(related_query.dst_node)
        if maintenance_queries_to_send:
            queries_to_send.extend(maintenance_queries_to_send)
        if exp_queries_to_send:
            datagrams = self._register_queries(exp_queries_to_send)
            datagrams_to_send.extend(datagrams)
        return queries_to_send

    def _announce(self, lookup_obj):
        queries_to_send, announce_to_myself = lookup_obj.announce()
        return queries_to_send
    '''
    if announce_to_myself:
    self._tracker.put(lookup_obj._info_hash,
    (self._my_node.addr[0], lookup_obj._bt_port))
    '''
    
    def _register_queries(self, queries_to_send, lookup_obj=None):
        if not queries_to_send:
            return []
        timeout_call_ts, datagrams_to_send = self._querier.register_queries(
            queries_to_send)
        self._next_main_loop_call_ts = min(self._next_main_loop_call_ts,
                                           timeout_call_ts)
        return datagrams_to_send
    
