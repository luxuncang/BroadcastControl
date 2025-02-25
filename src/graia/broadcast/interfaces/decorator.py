from typing import TYPE_CHECKING, Any, Dict

from ..entities.decorator import Decorator
from ..entities.dispatcher import BaseDispatcher
from ..entities.signatures import Force
from ..utilles import Ctx, run_always_await_safely

if TYPE_CHECKING:
    from ..interfaces.dispatcher import DispatcherInterface


ctx_dei_returnvalue = Ctx("ctx_dei_returnvalue")


class DecoratorInterface(BaseDispatcher):
    """Broadcast Control 内部机制 Decorator 的具体管理实现"""

    dispatcher_interface: "DispatcherInterface"

    def __init__(self, dispatcher_interface: "DispatcherInterface"):
        self.dispatcher_interface = dispatcher_interface

    @property
    def name(self):
        return self.dispatcher_interface.name

    @property
    def annotation(self):
        return self.dispatcher_interface.annotation

    @property
    def event(self):
        return self.dispatcher_interface.event

    @property
    def return_value(self):
        return ctx_dei_returnvalue.get()

    @property
    def local_storage(self):
        return self.dispatcher_interface.execution_contexts[-1].local_storage

    async def catch(self, interface: "DispatcherInterface"):
        if isinstance(interface.default, Decorator):
            decorator: Decorator = interface.default
            with ctx_dei_returnvalue.use(
                await interface.lookup_param(interface.name, interface.annotation, None, [[], 0])
                if not decorator.pre
                else None
            ):
                return Force(await run_always_await_safely(decorator.target, self))
