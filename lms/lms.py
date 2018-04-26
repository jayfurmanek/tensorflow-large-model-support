"""LMS
"""
import tensorflow as tf
import tensorflow.contrib.graph_editor as ge
from tensorflow.contrib.graph_editor import util

import time
from six.moves import queue as Queue
from lms import topos
from enum import Enum


class CTRLD_Strategy(Enum):
    CHAIN_RULE = 1
    DIRECT_ORDER = 2


class LMS(object):
    """LMS class for Large Model Support (LMS).

    The `LMS` object statically modifies a model by swapping its tensors
    to the host so that the model can be trained with the limited memory
    of GPUs.

    Tensors those are generated by forward operations and consumed by
    backward operations are candidates for swapping. The `LMS` object will
    automatically find these tensors.

    Swapping is done by cutting the link between a forward operation and
    its backward operation, then replacing the link by inserting `identity`
    operations on the host. In theory, this procedure does not have any
    effect on the training convergence as well as inference task.
    """
    def __init__(self, graph=None, optimizer_scopes=set(),
                 starting_scope=None,
                 excl_scopes=set(),
                 incl_scopes=set(),
                 excl_types=set(),
                 incl_types=set(),
                 lb=1, ub=10000,
                 n_tensors=-1,
                 fuse_swapins=False,
                 ctrld_strategy="chain_rule",
                 swap_branches=False,
                 branch_threshold=0,
                 debug=False,
                 debug_level=1,
                 cpu_device="/cpu:0"):
        """Create an LMS object to edit the graph for supporting large model.

        Args:
          graph: the graph we will modify for LMS. This should be the graph of
            user-defined neural network.
          optimizer_scopes: a set of scopes for the optimizers/solvers.
          starting_scope: tensors that are reachable from the operations in
            this scope will be swapped for LMS. Set this to the scope of the
            first layer if we would like to modify the whole graph.
          excl_scopes: a set of scopes for operations whose tensors will not
            be swapped out to the host. Default `empty`.
          incl_scopes: a set of scopes for operations whose tensors will be
            swapped out to the host. Default `empty`.
          excl_types: a set of types for operations whose tensors will not be
            swapped out to the host. Default `empty`.
          incl_types: a set of types for operations whose tensors will be
            swapped out to the host. Default `empty`.
          n_tensors: the number of tensors for LMS, counting from the
            `starting_scope`. To turn off LMS, set `n_tensors` to `0`.
            Default `-1` (all reachable tensors will be swapped for LMS).
          lb: lower-bound value for LMS. A tensor will be swapped in during the
            backward phase at least `lb` nodes before it in the graph.
            Default `1`.
          ub: upper-bound value for LMS. Default `10000`.
          fuse_swapins: Fuse "close" swap-in operations into one operation.
            This may improve the performance. Default `False`.
          debug: debug mode for LMS. Default `False`.
          debug_level: Debug level for LMS (1 or 2). Default `1`.
          cpu_device: the device we would like swap tensors to.
        """
        # TODO, throw an exception here, probably make optimizer_scopes
        # be a positional arg
        if not optimizer_scopes:
            self._log_info("set the optimizer scope")
            return

        self._graph = graph
        self._optimizer_scopes = optimizer_scopes
        self._excl_scopes = excl_scopes
        self._incl_scopes = incl_scopes
        self._excl_types = excl_types
        self._incl_types = incl_types
        self._starting_scope = starting_scope
        self._lb = lb
        self._ub = ub
        self._n_tensors = n_tensors
        self._fuse_swapins = fuse_swapins
        if ctrld_strategy == "chain_rule":
            self._ctrld_strategy = CTRLD_Strategy.CHAIN_RULE
        elif ctrld_strategy == "direct_order":
            self._ctrld_strategy = CTRLD_Strategy.DIRECT_ORDER
        else:
            self._ctrld_strategy = "chain_rule"

        self._swap_branches = swap_branches
        self._branch_threshold = branch_threshold

        # Operations with these types will be ignored
        atomic_types = {'Const', 'Mul', 'Add',
                        'Identity', 'Assign', 'VariableV2',
                        'Reshape', 'Shape', 'ShapeN', 'Placeholder'}
        self._excl_types |= atomic_types

        self._excl_ops = set()
        self._incl_ops = set()
        self._grad_ops = set()
        self._topo_sort = None
        self._cpu_device = cpu_device
        self._debug = debug
        self._debug_level = debug_level

        # keep log of tensors on host
        self._incpu_count = 0

        # store a dictionary of visited ops to avoid multiple visits
        self._ops_dict = {}

    def _build_gradient_ops(self):
        """Return a set of operations in the backward phase.

        Operations in the backward phase are determined by its scope.
        """
        for scope in self._optimizer_scopes:
            self._grad_ops.update(
                set(ge.filter_ops_from_regex(
                    ge.make_list_of_op(self._graph), "^{}".format(scope))))

    def _get_seed_ops(self):
        """Return a list of `tf.Operation` used as a starting point for LMS
        to traverse the graph.

        If a starting scope is given, the ops in this scope will be used.
        Otherwise, this method automatically searches for starting ops.
        """
        # seep ops for search
        seed_ops = None
        if self._starting_scope:
            seed_ops = ge.filter_ops_from_regex(
                ge.make_list_of_op(self._graph), "^{}".format(
                    self._starting_scope))
        else:
            candidates = set()
            for op in self._graph.get_operations():
                if op in self._grad_ops:
                    continue
                for t in op.outputs:
                    frontier_ops = set(util.get_consuming_ops(t))
                    if (frontier_ops & self._grad_ops):
                        candidates.add(op)
                        break

            # ordering an operation by how much it covers the other ops
            tmp_dict = {}
            max_nelems = -1
            for op in candidates:
                nelems = len(set(self._get_forward_walk_ops(op,
                                                            inclusive=False))
                             & candidates)
                if nelems > 0:
                    tmp_dict[op] = nelems
                    max_nelems = nelems if (nelems > max_nelems) else max_nelems

            # seed ops will cover most of the forward ops
            seed_ops = [k for k, v in tmp_dict.items() if v == max_nelems]
        return seed_ops

    def _filter_scopes_and_types(self, within_ops, scopes, types):
        """Filter out ops that are not in `scopes` and not of `types`.

        Args:
          within_ops: an object convertible to a list of `tf.Operation`.
          scopes: a list of scope path.
          types: a list of tf.DataType.
        Return:
          A set of `tf.Operation`.
        """
        ops = set()
        for scope in scopes:
            ops |= set(ge.get_name_scope_ops(within_ops, scope))
        ops |= {op
                for op in within_ops
                if op.type in types}
        return ops

    def _get_forward_walk_ops(self, op, inclusive=True):
        if op in self._ops_dict:
            if inclusive:
                return self._ops_dict[op]
            else:
                return list(set(self._ops_dict[op]) - {op})
        else:
            ret = ge.get_forward_walk_ops(op)
            self._ops_dict[op] = ret
            if inclusive:
                return ret
            else:
                return list(set(ret) - {op})

    def run(self, graph=None):
        """Edit the graph by adding swapin and swapout ops.

        Swapin and swapout ops are in the host.

        The graph is modified in-place.

        Return:
          a set of added ops.
        """
        if graph:
            self._graph = graph

        if not self._graph:
            self._log_info("Input graph: Not found")
            return

        if self._n_tensors == 0:
            self._log_info("Not modify model for LMS")
            return  # turn off LMS
        elif self._n_tensors < 0:
            self._n_tensors = 0  # swap all tensors (default)

        self._log_info("Editing model for LMS")
        self._print_configuration()
        start_time = time.time()

        self._build_gradient_ops()
        seed_ops = self._get_seed_ops()

        self._log_info(
            "Starting ops: {}".format(
                [(op.name, op.type) for op in seed_ops]), 1)

        reachable_ops = set()
        for seed_op in seed_ops:
            reachable_ops |= set(self._get_forward_walk_ops(seed_op))
        reachable_ops -= self._grad_ops

        # exclusive ops
        self._excl_ops = self._filter_scopes_and_types(reachable_ops,
                                                       self._excl_scopes,
                                                       self._excl_types)
        # inclusive ops
        self._incl_ops = self._filter_scopes_and_types(reachable_ops,
                                                       self._incl_scopes,
                                                       self._incl_types)

        # build a topological sort
        self._topo_sort = topos.TOPOS(seed_ops, self._grad_ops)
        self._topo_sort.build()
        for i in range(0, self._topo_sort.size):
            self._log_info("[{}]: {}".format(
                i, [op.name for op in self._topo_sort.get_ops(i)]), 1)

        self._do_action(seed_ops)

        # check the validation of the new model
        new_reachable_ops = set()
        for seed_op in seed_ops:
            new_reachable_ops |= set(ge.get_forward_walk_ops(seed_op))
        new_reachable_ops -= self._grad_ops
        if (new_reachable_ops >= reachable_ops):
            self._log_info("Edited model is valid and logically equivalent to the original one")
            self._log_info("Added {} ops into the model".format(len(new_reachable_ops - reachable_ops)))
        else:
            self._log_info("Edited model is invalid. Running this may produce unexpected result")

        self._log_info("Editing model for LMS, took: {} ms".format(
            (time.time()-start_time)*1000))
        self._log_info(
            "{} tensors will be swapped out(in) to(from) the host".format(
                self._incpu_count))
        return (new_reachable_ops - reachable_ops)

    def _do_action(self, src_ops):
        """Add swapin and swapout ops for ops that are reachable from `src_ops`.

        Args:
          src_ops: a list of `tf.Operation`
        """
        open_set = Queue.Queue()
        closed_set = set()

        for op in src_ops:
            open_set.put(op)

        while not open_set.empty():
            src_op = open_set.get()

            # get next ops before the graph is changed
            next_ops = set()
            for t in src_op.outputs:
                frontier_ops = set(util.get_consuming_ops(t))
                next_ops |= frontier_ops - self._grad_ops

            # do action for src_op
            self._insert_swap_nodes(src_op)

            for op in next_ops:
                if op in closed_set:
                    continue
                if op not in open_set.queue:
                    open_set.put(op)

            closed_set.add(src_op)

    def _fuse_swapin_ops(self, src_op, swapout_op, bw_frontier_ops, ts0):
        """Fuse all swapin ops that swaps in the same tensor.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          swapout_op: a `tf.Operation`.
          bw_frontier_ops: a set of `tf.Operation`.
          ts0: a `tf.Tensor`.

        Return:
          A set of `tf.Operation` that cannot be fused.
        """
        fuse_bw_frontier_ops = {
            op for op in bw_frontier_ops
            if self._topo_sort.get_order(op) > 0}
        if len(fuse_bw_frontier_ops) >= 2:
            with tf.device(self._cpu_device):
                swap_in = tf.identity(ts0)

            # Connect: swap_out -> swap_in
            self._connect_ops(swapout_op, swap_in.op)
            self._excl_ops.add(swap_in.op)

            # reuse swap_in tensors
            for op in fuse_bw_frontier_ops:
                # Connect: swap_in -> dest
                input_idx = ge.sgv(
                    op, graph=self._graph).input_index(ts0)
                self._connect_ops(swap_in.op, op, remap_inputs=True,
                                  idx=input_idx)

                self._log_info(
                    "{} (order {}) reuses tensor {}".format(
                        op.name,
                        self._topo_sort.get_order(op),
                        ts0.name),
                    1)

            # control dependency -> swap_in
            min_order = self._topo_sort.size + 1
            earliest_op = None
            for op in fuse_bw_frontier_ops:
                order = self._topo_sort.get_order(op)
                if order < min_order:
                    min_order = order
                    earliest_op = op
            if earliest_op:
                self._add_control_dependency(src_op, earliest_op, swap_in.op,
                                             self._lb, self._ub)
        return (bw_frontier_ops - fuse_bw_frontier_ops)

    def _get_branch_ops(self, within_ops, threshold=0):
        """Get ops whose order compared to the minimum order
        is greater than the threshold.

        Args:
          within_ops: a set of `tf.Operation`.
          threshold: an integer.

        Return:
          A set of `tf.Operation`.
        """
        orders = {self._topo_sort.get_order(op)
                  for op in within_ops}
        if not orders:
            return set()
        min_order = min(orders) + threshold
        branch_ops = {
            op
            for op in within_ops
            if (self._topo_sort.get_order(op) > min_order)}
        return branch_ops

    def _insert_swap_nodes(self, src_op):
        """Insert swapin and swapout ops for the given operation into the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`
        """
        self._log_info("Operation: {}".format(src_op), 2)

        # bypass excluded ops
        if src_op in self._excl_ops:
            return

        # if inclusive mode is enabled, only proceed if this op is included
        if self._incl_ops:
            if src_op not in self._incl_ops:
                return

        for t in src_op.outputs:
            if (self._n_tensors > 0) and (self._incpu_count >= self._n_tensors):
                return

            frontier_ops = set(util.get_consuming_ops(t))
            self._log_info("my frontier ops: {}".format(frontier_ops), 2)

            bw_frontier_ops = frontier_ops & self._grad_ops
            self._log_info("my bw frontier ops: {}".format(bw_frontier_ops), 2)

            # swap branch ops if they are far enough (depending on threshold)
            if self._swap_branches:
                fw_branch_ops = self._get_branch_ops(
                    frontier_ops - self._grad_ops,
                    self._branch_threshold)
                bw_frontier_ops = bw_frontier_ops | fw_branch_ops

            # Do not swap tensors used by bw ops without outgoing ops.
            # These bw ops can be removed by Tensorflow compiler
            bw_frontier_ops = {op
                               for op in bw_frontier_ops
                               if op.outputs}

            if not bw_frontier_ops:
                continue

            self._log_info("Operation: {}, order {}, type {}".format(
                src_op.name, self._topo_sort.get_order(src_op),
                src_op.type), 1)

            # create swap_out node
            swapout_op = self._add_swapout(src_op, t)
            self._incpu_count = self._incpu_count + 1

            # create swap_in nodes
            if self._fuse_swapins:
                bw_frontier_ops = self._fuse_swapin_ops(
                    src_op, swapout_op, bw_frontier_ops, t)
            for dest_op in bw_frontier_ops:
                # swap_in op
                swapin_op = self._add_swapin(swapout_op, dest_op, t)
                # control dependency -> swap_in
                self._add_control_dependency(src_op, dest_op, swapin_op,
                                             self._lb, self._ub)

    def _add_swapout(self, src_op, ts0):
        """Add a swapout operation to the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          ts0: a `tf.Tensor`.

        Return:
          A `tf.Operation`.
        """
        with tf.device(self._cpu_device):
            swap_out = tf.identity(ts0)

        # Connect: src-node -> swap-out
        src_svg = ge.sgv(src_op, graph=self._graph)
        src_out_idx = src_svg.output_index(ts0)
        self._connect_ops(src_op, swap_out.op, remap_outputs=True,
                          idx=src_out_idx)
        self._excl_ops.add(swap_out.op)
        self._log_info("Tensor {} will be placed on {}".format(
            ts0.name, self._cpu_device), 1)

        return swap_out.op

    def _add_swapin(self, swapout_op, dest_op, ts0):
        """Add a swapin operation to the graph.

        This method does an in-place modification to the graph.

        Args:
          swapout_op: a `tf.Operation`.
          dest_op: a `tf.Operation`.
          ts0: a `tf.Tensor`.

        Return:
          A `tf.Operation`.
        """
        with tf.device(self._cpu_device):
            swap_in = tf.identity(ts0)

        # Connect: swap_out -> swap_in
        self._connect_ops(swapout_op, swap_in.op)

        # Connect: swap_in -> dest
        dest_svg = ge.sgv(dest_op, graph=self._graph)
        input_idx = dest_svg.input_index(ts0)
        self._connect_ops(swap_in.op, dest_op, remap_inputs=True, idx=input_idx)
        self._excl_ops.add(swap_in.op)

        self._log_info("Consuming op {} (order {}) swaps in {}".format(
            dest_op.name, self._topo_sort.get_order(dest_op),
            ts0.name), 1)

        return swap_in.op

    def _add_control_dependency(self, fw_op, bw_op, swapin_op, lb, ub):
        """Find and add a control dependency to the graph.

        This method does an in-place modification to the graph.

        Args:
          fw_op: a `tf.Operation`.
          bw_op: a `tf.Operation`.
          swapin_op: a `tf.Operation`.
          lb: an `integer`.
          up: an `integer`.
        """
        if self._topo_sort.get_order(bw_op) < 0:
            nco = self._find_nco(fw_op, bw_op)
            if nco:
                bw_op = nco
            else:
                in_scope_ops = self._find_inscope(bw_op.name)
                if in_scope_ops:
                    bw_op = in_scope_ops
                else:
                    self._log_info("No control dependency op", 1)
                    return

        # if lb is out of range, reset it to make sure
        # that a control dependency op will be found
        if (self._topo_sort.get_order(bw_op) - lb
            <= self._topo_sort.get_order(fw_op)):
            lb = 1
        if self._ctrld_strategy is CTRLD_Strategy.CHAIN_RULE:
            re = self._do_chain_rule(fw_op, bw_op, lb, ub)
        elif self._ctrld_strategy is CTRLD_Strategy.DIRECT_ORDER:
            re = self._do_direct_order(fw_op, bw_op, lb, ub)
        else:
            re = self._do_chain_rule(fw_op, bw_op, lb, ub)

        ctrld_op = re[0]
        ctrld_order = re[1]
        if ctrld_op:
            ge.add_control_inputs(swapin_op, ctrld_op)
            self._log_info(
                "Control dependency op {},  order: {}".format(
                    ctrld_op.name, ctrld_order), 1)
        else:
            self._log_info("No control dependency op", 1)

    def _find_nco(self, fw_op, bw_op):
        """Find the nearest common ops in reachable ops of two given ops.

        Args:
          fw_op: a `tf.Operation`.
          bw_op: a `tf.Operation`.

        Return:
          A set of `tf.Operation`.
        """
        frontier_ops = set()
        for t in fw_op.outputs:
            frontier_ops |= set(util.get_consuming_ops(t))
        frontier_ops -= self._grad_ops
        fw_reachable_ops = {op2
                            for op1 in frontier_ops
                            for op2 in set(self._get_forward_walk_ops(op1))}

        bw_reachable_ops = set(self._get_forward_walk_ops(
            bw_op, inclusive=False))
        common_ops = fw_reachable_ops & bw_reachable_ops
        min_order = self._topo_sort.size + 1
        nco_op = None
        for op in common_ops:
            order = self._topo_sort.get_order(op)
            if order < 0:
                continue
            if order < min_order:
                min_order = order
                nco_op = op
        return nco_op

    def _find_inscope(self, scope):
        """Find the closest ops that are backward ops by expanding from the scope.

        Args:
          scope: a scope path.

        Return:
          A set of `tf.Operation`.
        """
        current_scope = scope
        higher_scope = current_scope.rsplit('/', 1)[0]

        visited_ops = set()
        while (current_scope != higher_scope):
            ops = set(ge.filter_ops_from_regex(
                ge.make_list_of_op(self._graph),
                "^{}".format(higher_scope)))

            # not consider inner ops
            ops1 = ops - visited_ops

            # gradient ops only
            ops1 &= self._grad_ops

            # ops in chain rule
            ops1 = {op for op in ops1 if self._topo_sort.get_order(op) > 0}

            # get the earliest op
            min_order = self._topo_sort.size + 1
            earliest_op = None
            for op in ops1:
                order = self._topo_sort.get_order(op)
                if order < min_order:
                    min_order = order
                    earliest_op = op
            if not earliest_op:
                # go outside
                visited_ops |= ops
                current_scope = higher_scope
                higher_scope = current_scope.rsplit('/', 1)[0]
            else:
                return earliest_op

    def _do_chain_rule(self, fw_op, bw_op, lower_b, upper_b):  # BFS
        """Find a control dependency operation using chain rules.
        Go down along the forward phase to find corresponding bw ops.

        Args:
          fw_op: a `tf.Operation`.
          bw_op: a `tf.Operation`.
          lower_b: an `integer`.
          upper_b: an `integer`.

        Return:
          A tuple of (`tf.Operation`, an `integer`).
        """
        fw_order = self._topo_sort.get_order(fw_op)
        bw_order = self._topo_sort.get_order(bw_op)

        # check if the bw op is near the boundary between fw and bw phases
        if (bw_order - lower_b) < self._topo_sort.bw_starting_order:
            return self._do_direct_order(fw_op, bw_op, lower_b, upper_b)

        open_set1 = Queue.Queue()
        open_set2 = Queue.Queue()
        closed_set = set()

        open_set1.put(fw_op)

        result_ops = set()
        while not open_set1.empty():
            # stop if reaching the upperbound
            if upper_b == 0 or (lower_b > upper_b):
                break

            src_op = open_set1.get()

            # do action for src_op
            total_consumming_ops = set()
            for t in src_op.outputs:
                consumming_ops = set(util.get_consuming_ops(t))
                total_consumming_ops |= consumming_ops

            if lower_b <= 0:
                # inside the range
                consumming_ops_bw = total_consumming_ops & self._grad_ops
                # check validation
                consumming_ops_bw = {
                    op
                    for op in consumming_ops_bw
                    if self._topo_sort.get_order(op) > fw_order}
                consumming_ops_bw = {
                    op
                    for op in consumming_ops_bw
                    if self._topo_sort.get_order(op) < bw_order}
                result_ops |= consumming_ops_bw
            # go to the next level
            next_ops = total_consumming_ops - self._grad_ops
            for op in next_ops:
                if op in closed_set:
                    continue
                if op not in open_set2.queue:
                    open_set2.put(op)

            closed_set.add(src_op)
            if open_set1.empty():
                if result_ops:
                    break
                lower_b = lower_b - 1
                upper_b = upper_b - 1
                while not open_set2.empty():
                    open_set1.put(open_set2.get())
        if result_ops:
            ctrld_op = next(iter(result_ops))
            return (ctrld_op, self._topo_sort.get_order(ctrld_op))
        else:
            return (None, -1)

    def _do_direct_order(self, fw_op, src_op, lower_b, upper_b):
        """Find a control dependency operation using topological sort.

        Args:
          fw_op: a `tf.Operation`.
          bw_op: a `tf.Operation`.
          lower_b: an `integer`.
          upper_b: an `integer`.

        Return:
          A tuple of (`tf.Operation`, `integer`).
        """
        result_ops = set()

        # offset ordering
        fw_order = self._topo_sort.get_order(fw_op)
        src_order = self._topo_sort.get_order(src_op)

        range_ub = src_order - lower_b
        range_lb = max([src_order - upper_b, fw_order]) + 1

        ctrld_order = -1
        for i in reversed(range(range_lb, range_ub)):
            candidates = self._topo_sort.get_ops(i)
            # on the chain rule path
            candidates = {op
                          for op in candidates
                          if src_op in set(self._get_forward_walk_ops(op))}
            if candidates:
                result_ops |= candidates
                ctrld_order = i
                break

        if result_ops:
            ctrld_op = next(iter(result_ops))
            return (ctrld_op, ctrld_order)
        else:
            return (None, -1)

    def _log_info(self, message, level=0):
        """Log debug information.

        Args:
          message: a formatted string.
          level: an `integer`.
        """
        if level == 0 or (self._debug and self._debug_level >= level):
            # Use tf.logging.info instead of print, since print
            # is not thread safe, which can break tests.
            tf.logging.info("[LMS][{}] {}".format(level, message))

    def _print_configuration(self):
        """Print configuration information about LMS.
        """
        if self._n_tensors == 0:
            self._log_info("n_tensors: all tensors")
        else:
            self._log_info("n_tensors: {}".format(self._n_tensors))
        self._log_info("lb: {}".format(self._lb))

    def _connect_ops(self, src_op, dest_op, remap_inputs=False,
                     remap_outputs=False, idx=None, disconnect_first=False):
        """A wrapper of `tensorflow.contrib.graph_editor.connect`.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          dest_op: a `tf.Operation`.
          remap_inputs: remap the input of `dest_op` or not.
          remap_outputs: remap the output of `src_op` or not.
          idx: index of input or output tensor.
          disconnect_first: True means the current outputs of sgv0 are
            disconnected.
        """
        src_sgv = ge.sgv(src_op, graph=self._graph)
        dest_sgv = ge.sgv(dest_op, graph=self._graph)
        if remap_outputs:
            src_sgv = src_sgv.remap_outputs([idx])
        if remap_inputs:
            dest_sgv = dest_sgv.remap_inputs([idx])

        ge.connect(src_sgv, dest_sgv, disconnect_first)
