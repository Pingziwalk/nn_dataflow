""" $lic$
Copyright (C) 2016-2017 by The Board of Trustees of Stanford University

This program is free software: you can redistribute it and/or modify it under
the terms of the Modified BSD-3 License as published by the Open Source
Initiative.

If you use this program in your research, we request that you reference the
TETRIS paper ("TETRIS: Scalable and Efficient Neural Network Acceleration with
3D Memory", in ASPLOS'17. April, 2017), and that you send us a citation of your
work.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the BSD-3 License for more details.

You should have received a copy of the Modified BSD-3 License along with this
program. If not, see <https://opensource.org/licenses/BSD-3-Clause>.
"""

import argparse
import json
import multiprocessing
import sys
from collections import OrderedDict

from nn_dataflow import __version__ as VERSION
from nn_dataflow import NNDataflow
from nn_dataflow import Cost
from nn_dataflow import DataCategoryEnum as de
from nn_dataflow import MapStrategyEyeriss
from nn_dataflow import MemHierEnum as me
from nn_dataflow import NodeRegion
from nn_dataflow import Option
from nn_dataflow import PhyDim2
from nn_dataflow import Resource

from examples import import_network

def stats_dict(dfsch, cost):
    '''
    Get the stats as an OrderedDict from the NNDataflowScheme.
    '''
    stats = OrderedDict()

    ## Basic stats.

    stats['total_cost'] = dfsch.total_cost
    stats['total_time'] = dfsch.total_time

    stats['total_ops'] = dfsch.total_ops
    stats['total_accesses'] = dfsch.total_accesses
    stats['total_noc_hops'] = dfsch.total_noc_hops

    ## Cost breakdown.

    total_op_cost = dfsch.total_ops * cost.mac_op
    total_access_cost = sum(a * c for a, c
                            in zip(dfsch.total_accesses, cost.mem_hier))
    total_noc_cost = dfsch.total_noc_hops * cost.noc_hop
    total_static_cost = dfsch.total_node_time * cost.unit_static

    sum_cost = total_op_cost + total_access_cost + total_noc_cost \
            + total_static_cost
    assert abs(sum_cost / dfsch.total_cost - 1) < 0.001

    stats['total_op_cost'] = total_op_cost
    stats['total_access_cost'] = total_access_cost
    stats['total_noc_cost'] = total_noc_cost
    stats['total_static_cost'] = total_static_cost

    ## Other stats.

    stats['active_node_pes'] = dfsch.perlayer_stats('active_node_pes')
    stats['total_dram_bandwidth'] = dfsch.perlayer_stats('total_dram_bandwidth')
    stats['schedules'] = dfsch.res_dict

    return stats


def do_scheduling(args):
    '''
    Get optimal scheduling for given problem. Return a result schedule.
    '''

    ## Network.

    network = import_network(args.net)
    batch_size = args.batch

    ## Resource.

    dim_nodes = PhyDim2(*args.nodes)
    dim_array = PhyDim2(*args.array)

    # Sizes of gbuf and regf are in words.
    word = (args.word + 7) / 8
    size_gbuf = args.gbuf / word
    size_regf = args.regf / word

    if args.mem_type == '2D':
        # Memory nodes are on two sides.
        mem_regions = (NodeRegion(dim=PhyDim2(h=dim_nodes.h, w=1),
                                  origin=PhyDim2(h=0, w=0)),
                       NodeRegion(dim=PhyDim2(h=dim_nodes.h, w=1),
                                  origin=PhyDim2(h=0, w=dim_nodes.w - 1)))
    elif args.mem_type == '3D':
        # All nodes have memory.
        mem_regions = (NodeRegion(dim=dim_nodes, origin=PhyDim2(0, 0)),)

    resource = Resource(dim_nodes=dim_nodes,
                        dim_array=dim_array,
                        mem_regions=mem_regions,
                        size_gbuf=size_gbuf,
                        size_regf=size_regf)

    ## Cost.

    hier_cost = [0] * me.NUM
    hier_cost[me.DRAM] = args.hier_cost[0]
    hier_cost[me.GBUF] = args.hier_cost[1]
    hier_cost[me.ITCN] = args.hier_cost[2]
    hier_cost[me.REGF] = args.hier_cost[3]
    cost = Cost(mac_op=args.op_cost,
                mem_hier=tuple(hier_cost),
                noc_hop=args.hop_cost,
                unit_static=args.unit_static_cost)

    ## Options.

    bypass = [True] * de.NUM
    bypass[de.IFM] = 'i' not in args.disable_bypass
    bypass[de.OFM] = 'o' not in args.disable_bypass
    bypass[de.FIL] = 'f' not in args.disable_bypass
    options = Option(sw_gbuf_bypass=tuple(bypass),
                     sw_solve_loopblocking=args.solve_loopblocking,
                     partition_hybrid=args.hybrid_partition,
                     partition_batch=args.batch_partition,
                     partition_ifmaps=args.ifmaps_partition,
                     partition_interlayer=args.interlayer_partition,
                     ntops=args.top,
                     nprocesses=args.processes,
                     verbose=args.verbose)

    ## Search schedules.

    nnd = NNDataflow(network, batch_size, resource, cost, MapStrategyEyeriss)
    tops, cache_stats = nnd.schedule_search(options)

    if not tops:
        sys.stderr.write('No valid dataflow found.\n')
        return None

    top = tops[0]

    ## Write results.

    res_map = OrderedDict()

    res_map['version'] = VERSION

    res_map['net'] = args.net
    res_map['batch'] = args.batch

    res_map['resource'] = resource._asdict()
    res_map['cost'] = cost._asdict()
    res_map['options'] = options._asdict()

    res_map['cache_stats'] = cache_stats

    stats = stats_dict(top, cost)
    for key, val in stats.items():
        res_map[key] = val

    return res_map


def main(args):
    ''' Main function. '''
    json.dump(do_scheduling(args), sys.stdout, indent=2,
              default=lambda _: None)
    sys.stdout.write('\n')
    return 0


def argparser():
    ''' Argument parser. '''

    ap = argparse.ArgumentParser()

    ap.add_argument('net',
                    help='network name, should be a .py file under examples')

    ap.add_argument('--batch', type=int, required=True,
                    help='batch size')
    ap.add_argument('--word', type=int, default=16,
                    help='word size in bits')

    ap.add_argument('--nodes', type=int, nargs=2, required=True,
                    metavar=('H', 'W'),
                    help='Parallel node partitioning dimensions')
    ap.add_argument('--array', type=int, nargs=2, required=True,
                    metavar=('H', 'W'),
                    help='PE array dimensions')

    ap.add_argument('--regf', type=int, required=True,
                    help='register file size in bytes per PE')
    ap.add_argument('--gbuf', type=int, required=True,
                    help='global buffer size in bytes')

    ap.add_argument('--op-cost', type=float, default=1,
                    help='cost of arithmetic operation')
    ap.add_argument('--hier-cost', type=float, nargs=4, default=[200, 6, 2, 1],
                    metavar=('DRAM_COST', 'GBUF_COST', 'ITCN_COST',
                             'REGF_COST'),
                    help='cost of access to memory hierarchy')
    ap.add_argument('--hop-cost', type=float, default=100,
                    help='cost of access through one NoC hop')
    ap.add_argument('--unit-static-cost', type=float, default=0,
                    help='static cost for unit execution time')

    ap.add_argument('--mem-type', default='2D', choices=['2D', '3D'],
                    help='memory type. "2D" has memory only on edge nodes; '
                         '"3D" has memory vertially on top of all nodes.')

    ap.add_argument('--disable-bypass', nargs='*', default=[],
                    choices=['i', 'o', 'f'],
                    help='whether disallowing gbuf bypass for i (input), o '
                         '(output), or f (filter)')
    ap.add_argument('--solve-loopblocking', action='store_true',
                    help='Use analytical solver to choose loop blocking. '
                         'Otherwise use exhaustive search.')

    ap.add_argument('--hybrid-partition',
                    '--hybrid-partition2d',  # deprecated old name
                    action='store_true',
                    help='Use hybrid partition for layer for node mapping. '
                         'Otherwise use naive method based on layer type.')
    ap.add_argument('--batch-partition', action='store_true',
                    help='Allow partitioning batch, i.e., consider data '
                         'parallelism.')
    ap.add_argument('--ifmaps-partition', '--ifmap-partition',
                    action='store_true',
                    help='Allow partitioning ifmap channel dimension, which '
                         'requires extra data synchronization.')
    ap.add_argument('--interlayer-partition', '--inter-layer-partition',
                    action='store_true',
                    help='Allow partitioning resources across multiple layers '
                         'and process them simultaneously as an inter-layer '
                         'pipeline.')

    ap.add_argument('-t', '--top', type=int, default=1,
                    help='Number of top schedules to keep during search.')
    ap.add_argument('-p', '--processes', type=int,
                    default=multiprocessing.cpu_count()/2,
                    help='Number of parallel processes to use for search.')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='Show progress and details.')

    return ap


if __name__ == '__main__':
    sys.exit(main(argparser().parse_args()))

