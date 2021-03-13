from inspect import isclass, isfunction, ismethod
import itertools
from functools import lru_cache, partial

from typing import (
    Any,
    Dict,
    Generator,
    List,
    Optional,
    TYPE_CHECKING,
    Tuple,
)
from graia.broadcast.entities.dispatcher import BaseDispatcher
from graia.broadcast.entities.event import BaseEvent

from graia.broadcast.entities.context import ExecutionContext, ParameterContext
from graia.broadcast.entities.signatures import Force
from graia.broadcast.entities.track_log import TrackLogType
from graia.broadcast.exceptions import RequirementCrashed
from graia.broadcast.typing import T_Dispatcher, T_Dispatcher_Callable

from ..utilles import NestableIterable, run_always_await_safely, cached_getattr

if TYPE_CHECKING:
    from graia.broadcast import Broadcast


DEFAULT_LIFECYCLE_NAMES = (
    "beforeDispatch",
    "afterDispatch",
    "beforeExecution",
    "afterExecution",
    "beforeTargetExec",
    "afterTargetExec",
)


class EmptyEvent(BaseEvent):
    class Dispatcher(BaseDispatcher):
        @staticmethod
        def catch(_):
            pass


class DispatcherInterface:
    broadcast: "Broadcast"

    execution_contexts: List["ExecutionContext"]
    parameter_contexts: List["ParameterContext"]

    track_logs: List[Tuple[TrackLogType, Any]] = None

    @staticmethod
    @lru_cache(None)
    def get_lifecycle_refs(
        dispatcher: "T_Dispatcher",
    ) -> Optional[Dict[str, List]]:
        from graia.broadcast.entities.dispatcher import BaseDispatcher

        lifecycle_refs: Dict[str, List] = {}
        if not isinstance(dispatcher, (BaseDispatcher, type)):
            return

        for name in DEFAULT_LIFECYCLE_NAMES:
            lifecycle_refs.setdefault(name, [])
            abstract_lifecycle_func = cached_getattr(BaseDispatcher, name)
            unbound_attr = getattr(dispatcher, name, None)

            if unbound_attr is None:
                continue

            orig_call = unbound_attr
            while ismethod(orig_call):
                orig_call = unbound_attr.__func__

            if orig_call is abstract_lifecycle_func:
                continue

            lifecycle_refs[name].append(unbound_attr)

        return lifecycle_refs

    def dispatcher_pure_generator(self) -> Generator[None, None, T_Dispatcher]:
        return itertools.chain(
            self.execution_contexts[0].dispatchers,
            self.parameter_contexts[-1].dispatchers,
            self.execution_contexts[-1].dispatchers,
        )

    def flush_lifecycle_refs(
        self,
        dispatchers: List["T_Dispatcher"] = None,
    ):
        from graia.broadcast.entities.dispatcher import BaseDispatcher

        lifecycle_refs = self.execution_contexts[-1].lifecycle_refs
        if dispatchers is None and lifecycle_refs:  # 已经刷新.
            return

        for dispatcher in dispatchers or self.dispatcher_pure_generator():
            if (
                not isinstance(dispatcher, BaseDispatcher)
                or dispatcher.__class__ is type
            ):
                continue

            for name, value in self.get_lifecycle_refs(dispatcher).items():
                lifecycle_refs.setdefault(name, [])
                lifecycle_refs[name].extend(value)

    async def exec_lifecycle(self, lifecycle_name: str, *args, **kwargs):
        lifecycle_funcs = self.execution_contexts[-1].lifecycle_refs.get(
            lifecycle_name, []
        )
        if lifecycle_funcs:
            for func in lifecycle_funcs:
                await run_always_await_safely(func, self, *args, **kwargs)

    def inject_local_raw(self, *dispatchers: List["T_Dispatcher"]):
        # 为什么没有 flush: 因为这里的 lifecycle 是无意义的.
        for dispatcher in dispatchers[::-1]:
            self.parameter_contexts[-1].dispatchers.insert(0, dispatcher)

    def inject_execution_raw(self, *dispatchers: List["T_Dispatcher"]):
        for dispatcher in dispatchers:
            self.execution_contexts[-1].dispatchers.insert(0, dispatcher)

        self.flush_lifecycle_refs(dispatchers)

    def inject_global_raw(self, *dispatchers: List["T_Dispatcher"]):
        # self.dispatchers.extend(dispatchers)
        for dispatcher in dispatchers[::-1]:
            self.execution_contexts[0].dispatchers.insert(1, dispatcher)

        self.flush_lifecycle_refs(dispatchers)

    @property
    def name(self) -> str:
        return self.parameter_contexts[-1].name

    @property
    def annotation(self) -> Any:
        return self.parameter_contexts[-1].annotation

    @property
    def default(self) -> Any:
        return self.parameter_contexts[-1].default

    @property
    def _index(self) -> int:
        return self.execution_contexts[-1]._index

    @property
    def event(self) -> BaseEvent:
        return self.execution_contexts[-1].event

    @property
    def global_dispatcher(self) -> List[T_Dispatcher]:
        return self.execution_contexts[0].dispatchers

    @property
    def current_path(self) -> NestableIterable[int, T_Dispatcher]:
        return self.parameter_contexts[-1].path

    @property
    def has_current_exec_context(self) -> bool:
        return len(self.execution_contexts) >= 2

    @property
    def has_current_param_context(self) -> bool:
        return len(self.parameter_contexts) >= 2

    def __init__(self, broadcast_instance: "Broadcast") -> None:
        self.broadcast = broadcast_instance
        self.execution_contexts = [ExecutionContext([], EmptyEvent())]
        self.parameter_contexts = [
            ParameterContext(
                None,
                None,
                None,
                [],
                [
                    [],
                ],
            )
        ]

    async def __aenter__(self) -> "DispatcherInterface":
        return self

    async def __aexit__(self, _, exc: Exception, tb):
        await self.exit_current_execution()
        if tb is not None:
            raise exc.with_traceback(tb)

    def start_execution(
        self,
        event: BaseEvent,
        dispatchers: List[T_Dispatcher],
        track_log_receiver: List[Tuple[TrackLogType, Any]] = None,
    ) -> "DispatcherInterface":
        self.execution_contexts.append(ExecutionContext(dispatchers, event))
        self.flush_lifecycle_refs()
        if track_log_receiver is not None:
            self.track_logs = track_log_receiver
        else:
            self.track_logs = []
        return self

    async def exit_current_execution(self):
        self.execution_contexts.pop()
        self.track_logs = None

    @staticmethod
    @lru_cache(None)
    def dispatcher_callable_detector(dispatcher: T_Dispatcher) -> T_Dispatcher_Callable:
        if hasattr(dispatcher, "catch"):
            if isfunction(dispatcher.catch) or not isclass(dispatcher):
                return dispatcher.catch
            else:
                return partial(dispatcher.catch, None)
        elif callable(dispatcher):
            return dispatcher
        else:
            raise ValueError("invaild dispatcher: ", dispatcher)

    def init_dispatch_path(self) -> List[List["T_Dispatcher"]]:
        return [
            [],
            self.execution_contexts[0].dispatchers,
            self.parameter_contexts[-1].dispatchers,
            self.execution_contexts[-1].dispatchers,
        ]

    async def lookup_param_without_log(
        self,
        name: str,
        annotation: Any,
        default: Any,
        using_path: List[List["T_Dispatcher"]] = None,
    ) -> Any:
        self.parameter_contexts.append(
            ParameterContext(
                name, annotation, default, [], using_path or self.init_dispatch_path()
            )
        )

        result = None
        try:
            for dispatcher in self.current_path:
                result = await run_always_await_safely(
                    self.dispatcher_callable_detector(dispatcher), self
                )

                if result is None:
                    continue

                if result.__class__ is Force:
                    result = result.target

                self.execution_contexts[-1]._index = 0
                return result
            else:
                raise RequirementCrashed(
                    "the dispatching requirement crashed: ",
                    self.name,
                    self.annotation,
                    self.default,
                )
        finally:
            self.parameter_contexts.pop()

    async def lookup_param(
        self,
        name: str,
        annotation: Any,
        default: Any,
        using_path: List[List["T_Dispatcher"]] = None,
    ) -> Any:
        self.parameter_contexts.append(
            ParameterContext(
                name, annotation, default, [], using_path or self.init_dispatch_path()
            )
        )

        result = None
        self.track_logs.append((TrackLogType.LookupStart, name, annotation, default))
        try:
            for dispatcher in self.current_path:
                result = await run_always_await_safely(
                    self.dispatcher_callable_detector(dispatcher), self
                )

                if result is None:
                    self.track_logs.append((TrackLogType.Continue, name, dispatcher))
                    continue

                if result.__class__ is Force:
                    result = result.target

                self.track_logs.append((TrackLogType.Result, name, dispatcher))
                self.execution_contexts[-1]._index = 0
                return result
            else:
                self.track_logs.append((TrackLogType.RequirementCrashed, name))
                raise RequirementCrashed(
                    "the dispatching requirement crashed: ",
                    self.name,
                    self.annotation,
                    self.default,
                )
        finally:
            self.track_logs.append((TrackLogType.LookupEnd, name))
            self.parameter_contexts.pop()

    async def lookup_using_current(self) -> Any:
        result = None
        for dispatcher in self.current_path:
            result = await run_always_await_safely(
                self.dispatcher_callable_detector(dispatcher), self
            )
            if result is None:
                continue

            if result.__class__ is Force:
                result = result.target

            self.execution_contexts[-1]._index = 0
            return result
        else:
            raise RequirementCrashed(
                "the dispatching requirement crashed: ",
                self.name,
                self.annotation,
                self.default,
            )

    async def lookup_by_directly(
        self,
        dispatcher: T_Dispatcher,
        name: str,
        annotation: Any,
        default: Any,
        using_path: List[List["T_Dispatcher"]] = None,
    ) -> Any:
        self.parameter_contexts.append(
            ParameterContext(
                name, annotation, default, [], using_path or self.init_dispatch_path()
            )
        )

        dispatcher_callable = self.dispatcher_callable_detector(dispatcher)

        try:
            result = await run_always_await_safely(dispatcher_callable, self)
            if result.__class__ is Force:
                return result.target

            return result
        finally:
            self.parameter_contexts.pop()
