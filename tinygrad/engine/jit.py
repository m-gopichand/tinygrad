from __future__ import annotations
from typing import TypeVar, Generic, Callable, List, Tuple, Union, Dict, cast, Optional, Any
import functools, itertools, collections
from tinygrad.tensor import Tensor
from tinygrad.lazy import LazyBuffer
from tinygrad.helpers import flatten, merge_dicts, DEBUG, Context, GRAPH, BEAM, getenv, all_int, GraphException, colored
from tinygrad.device import Buffer, CompiledRunner, BufferXfer, Compiled, Device, Runner
from tinygrad.dtype import DType
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.symbolic import Variable, sint
from tinygrad.engine.realize import ExecItem, capturing, _internal_memory_planner, EmptyOp, ViewOp
from tinygrad.nn.state import get_parameters
from weakref import WeakKeyDictionary

def apply_graph_to_jit(jit_cache: List[ExecItem], input_rawbuffers: List[Buffer], var_vals: Dict[Variable, int]) -> List[ExecItem]:
  # Split JIT cache into batches for faster graph execution.
  # This allows the accelerator to run some batches while subsequent graphs are still being updated.
  max_batch_size = getenv("JIT_BATCH_SIZE", 32)
  graphed_jit_cache: List[ExecItem] = []
  current_batch: List[ExecItem] = []
  current_device: Optional[Compiled] = None

  def flush_batch():
    nonlocal current_batch, current_device, max_batch_size
    try:
      if len(current_batch) <= 1 or current_device is None: raise GraphException("only one kernel doesn't graph")
      graph_runner = current_device.graph(current_batch, input_rawbuffers, var_vals)
      # clear jit inputs to allow their memory to be freed/reused
      for (j,i) in graph_runner.input_replace.keys(): graph_runner.jit_cache[j].bufs[i] = None
      graphed_jit_cache.append(ExecItem(graph_runner, cast(List[Optional[Buffer]], input_rawbuffers)))
      max_batch_size *= 2
      if DEBUG >= 2: print(f"\tJIT GRAPHing batch with {len(current_batch)} kernels on device {current_device}")
    except GraphException as e:
      graphed_jit_cache.extend(current_batch)
      if DEBUG >= 2: print(f"\tJIT GRAPHing failed batch with {len(current_batch)} kernels on device {current_device}: {e}")
    current_batch = []
    current_device = None

  for ji in jit_cache:
    if ji.prg.__class__ in {EmptyOp, ViewOp}: continue
    ji_graph_dev: Optional[Compiled] = None # device on which the ji will be graphed. Not graphed if None.
    if isinstance(ji.prg, CompiledRunner): ji_graph_dev = ji.prg.device
    elif isinstance(ji.prg, BufferXfer) and ji.bufs[0] and ji.bufs[0].device.split(":", 1)[0] in {"HSA", "CUDA"}:
      ji_graph_dev = Device[ji.bufs[0].device]

    can_be_graphed = ji_graph_dev and ji_graph_dev.graph
    can_extend_graph_batch = can_be_graphed and len(current_batch) < max_batch_size and (ji_graph_dev == current_device or
      (isinstance(ji_graph_dev.graph, type) and issubclass(ji_graph_dev.graph, MultiGraphRunner) and type(ji_graph_dev) == type(current_device))) #type:ignore
    if not can_extend_graph_batch and len(current_batch) > 0: flush_batch()

    if can_be_graphed: current_batch.append(ji)
    else: graphed_jit_cache.append(ji)

    current_device = ji_graph_dev

  if len(current_batch) > 0: flush_batch()
  return graphed_jit_cache

def get_input_replace(jit_cache: List[ExecItem], input_rawbuffers:List[Buffer]) -> Dict[Tuple[int, int], int]:
  input_replace: Dict[Tuple[int, int], int] = {}
  for j,ji in enumerate(jit_cache):
    for i,a in enumerate(ji.bufs):
      if a in input_rawbuffers:
        input_replace[(j,i)] = input_rawbuffers.index(a)
  return input_replace

class GraphRunner(Runner):  # pylint: disable=abstract-method
  def __init__(self, jit_cache: List[ExecItem], input_rawbuffers: List[Buffer], var_vals: Dict[Variable, int]):
    self.jit_cache = jit_cache
    self.input_replace = get_input_replace(jit_cache, input_rawbuffers)
    self.jc_idx_with_updatable_launch_dims = []
    self.jc_idx_with_updatable_var_vals = []
    op_estimate: sint = 0
    mem_estimate: sint = 0
    for j,ji in enumerate(jit_cache):
      op_estimate += ji.prg.op_estimate
      mem_estimate += ji.prg.mem_estimate
      if isinstance(ji.prg, CompiledRunner):
        if ji.prg.vars: self.jc_idx_with_updatable_var_vals.append(j)
        if (ji.prg.global_size and not all_int(ji.prg.global_size)) or (ji.prg.local_size and not all_int(ji.prg.local_size)):
          self.jc_idx_with_updatable_launch_dims.append(j)
    self.vars = list(var_vals.keys())
    super().__init__(colored(f"<batched {len(self.jit_cache)}>", "cyan"), jit_cache[0].prg.dname.split(":")[0], op_estimate, mem_estimate)

class MultiGraphRunner(GraphRunner):  # pylint: disable=abstract-method
  def __init__(self, jit_cache: List[ExecItem], input_rawbuffers: List[Buffer], var_vals: Dict[Variable, int]):
    self.w_dependency_map: Dict[Any, Any] = {}
    self.r_dependency_map: Dict[Any, List[Any]] = collections.defaultdict(list)
    super().__init__(jit_cache, input_rawbuffers, var_vals)

  def _access_resources(self, read, write, new_dependency:Any):
    # To synchronize access to resources, we monitor the necessary prerequisites for accessing each resource,
    # whether for write or read operations. A resource can be accessed by either a single writer or multiple readers.
    wait_nodes = []

    for rawbuf in read + write:
      if id(rawbuf._buf) in self.w_dependency_map: wait_nodes.append(self.w_dependency_map[id(rawbuf._buf)])
    for rawbuf in write:
      if id(rawbuf._buf) in self.r_dependency_map: wait_nodes.extend(self.r_dependency_map.pop(id(rawbuf._buf)))

    for rawbuf in read: self.r_dependency_map[id(rawbuf._buf)].append(new_dependency)
    for rawbuf in write: self.w_dependency_map[id(rawbuf._buf)] = new_dependency
    return list({id(x):x for x in wait_nodes}.values())

ReturnType = TypeVar('ReturnType')
class TinyJit(Generic[ReturnType]):
  def __init__(self, fxn:Callable[..., ReturnType]):
    self.fxn = fxn
    self.reset()

  def add_buffer(self, b:Buffer) -> Buffer:
    if found:=self.buffer_replace.get(b, None): return found
    if b.is_allocated() or b.lb_refcount > 0: return b
    if b._base is not None:
      self.buffer_replace[b] = ret = Buffer(b.device, b.size, b.dtype, base=self.buffer_replace.get(b._base, b._base), offset=b.offset)
    else:
      self.buffer_replace[b] = ret = Buffer(b.device, b.size, b.dtype, options=b.options)
    return ret

  def add(self, ei:ExecItem):
    self.jit_cache.append(ExecItem(ei.prg, [self.add_buffer(buf) for buf in ei.bufs if buf is not None]))

  def reset(self):
    self.jit_cache: List[ExecItem] = []
    self.input_replace: Dict[Tuple[int, int], int] = {}
    self.extra_view_inputs: List[Tuple[int, int, str, int, DType]] = []
    self.buffer_replace: WeakKeyDictionary[Buffer, Buffer] = WeakKeyDictionary()
    self.cnt: int = 0

  def __get__(self, obj, objtype): return functools.partial(self.__call__, obj) # add support for instance methods

  def __call__(self, *args, **kwargs) -> ReturnType:
    input_tensors: List[Tuple[Union[int, str], Tensor]] = \
      [(cast(Union[int, str], k),v) for k,v in itertools.chain(enumerate(args), sorted(kwargs.items())) if v.__class__ is Tensor]
    if len(input_tensors): Tensor.realize(*[x[1] for x in input_tensors])
    lbs: List[LazyBuffer] = flatten([v.lazydata.lbs for _,v in input_tensors])
    expected_sts_var_dtype_device = [(*x.st.unbind(), x.dtype, x.device) for x in lbs]
    input_rawbuffers: List[Buffer] = [v.base.realized for v in lbs if v.base.realized is not None]
    assert len(set(input_rawbuffers)) == len(input_rawbuffers), "duplicate inputs to JIT"
    var_vals: Dict[Variable, int] = merge_dicts([x[1] for x in expected_sts_var_dtype_device] + \
                                                [dict(x.unbind() for x in itertools.chain(args, kwargs.values()) if isinstance(x, Variable))])

    expected_names, expected_lbs = [x[0] for x in input_tensors], [(x[0], tuple(x[1].keys()), x[2], x[3]) for x in expected_sts_var_dtype_device]
    if self.cnt == 0:
      # jit ignore
      with Context(BEAM=0 if getenv("IGNORE_JIT_FIRST_BEAM") else BEAM.value):
        self.ret = self.fxn(*args, **kwargs)
        if len(params:=get_parameters(self.ret)): Tensor.realize(params[0], *params[1:])
    elif self.cnt == 1:
      # jit capture
      self.expected_names: List[Union[int, str]] = expected_names
      self.expected_lbs: List[Tuple[ShapeTracker, Tuple[Variable, ...], DType, str]] = expected_lbs
      with Context(GRAPH=getenv("JITGRAPH", GRAPH.value)):
        capturing.append(self)
        self.ret = self.fxn(*args, **kwargs)
        if len(params:=get_parameters(self.ret)): Tensor.realize(params[0], *params[1:])
        capturing.clear()
      del self.buffer_replace
      assert len(self.jit_cache), "didn't JIT anything!"
      if DEBUG >= 1: print(f"JIT captured {len(self.jit_cache)} kernels with {len(input_rawbuffers)} inputs")

      # track inputs that are views of buffers
      for ji in self.jit_cache:
        for b in ji.bufs:
          if b is not None and b._base is not None and b._base in input_rawbuffers:
            input_rawbuffers.append(b)
            self.extra_view_inputs.append((input_rawbuffers.index(b.base), b.offset, b.device, b.size, b.dtype))

      # memory planning (optional)
      assigned = _internal_memory_planner([cast(List[Buffer], x.bufs) for x in self.jit_cache], debug_prefix="JIT ")
      self.jit_cache = [ExecItem(ei.prg, [assigned.get(x,x).ensure_allocated() for x in ei.bufs if x is not None]) for ei in self.jit_cache]

      # Condense the items into a graph executor.
      if getenv("JIT") != 2: self.jit_cache = apply_graph_to_jit(self.jit_cache, input_rawbuffers, var_vals)

      self.input_replace = get_input_replace(self.jit_cache, input_rawbuffers)
      if DEBUG >= 1 and len(set(self.input_replace.values())) != len(input_rawbuffers): print("WARNING: some input tensors not found")
    elif self.cnt >= 2:
      # jit exec
      assert self.expected_names == expected_names and self.expected_lbs == expected_lbs, "args mismatch in JIT"
      for idx, offset, device, size, dtype in self.extra_view_inputs:
        input_rawbuffers.append(Buffer(device, size, dtype, base=input_rawbuffers[idx], offset=offset).ensure_allocated())
      for (j,i),input_idx in self.input_replace.items(): self.jit_cache[j].bufs[i] = input_rawbuffers[input_idx]
      if DEBUG >= 1 and len(self.jit_cache) >= 10: print(f"jit execs {len(self.jit_cache)} kernels")
      for ei in self.jit_cache: ei.run(var_vals, jit=True)

    # clear jit inputs
    for (j,i) in self.input_replace.keys(): self.jit_cache[j].bufs[i] = None

    self.cnt += 1
    return self.ret
