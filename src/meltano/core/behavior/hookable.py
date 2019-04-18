import contextlib
import logging

from collections import OrderedDict


class TriggerError(Exception):
    def __init__(self, hook, err):
        self.hook = hook
        self.error = err

    def __str__(self):
        return f"[{self.hook}]: {self.error}"


class hook:
    """
    This decorator marks a function as a __hook__.
    It will be found by the Hookable metaclass and
    registered to be triggered accordingly.
    """

    def __init__(self, hook_name, can_fail=False):
        self.name = hook_name
        self.can_fail = can_fail

    def __call__(self, func):
        func.__hook__ = self
        return func


class Hookable(type):
    """
    Metaclass that registers @hook functions into __hooks__
    Hooks are registered in declaration order.
    """

    def __new__(metacls, name, bases, dct):
        cls = type.__new__(metacls, name, bases, dct)
        cls.__hooks__ = {}

        for hook_name, hook in (
            (func.__hook__.name, func) for func in dct.values() if hasattr(func, "__hook__")
        ):
            cls.__hooks__[hook_name] = cls.__hooks__.get(hook_name, [])
            cls.__hooks__[hook_name].append(hook)

        return cls

    def __prepare__(name, bases, **kwds):
        return OrderedDict()


class HookObject(metaclass=Hookable):
    """
    Hook base class that handles the triggering of hooks.
    Hooks are triggered in reverse MRO order, which means
    derived classes hooks are called after their base class.
    """

    @contextlib.contextmanager
    def trigger_hooks(self, hook_name, *args, **kwargs):
        self.__class__.trigger(
            self, f"before_{hook_name}", *args, **kwargs
        )

        yield

        self.__class__.trigger(
            self, f"after_{hook_name}", *args, **kwargs
        )

    @classmethod
    def trigger(cls, target, hook_name, *args, **kwargs):
        hooks = [
            hook
            for hook_cls in reversed(cls.__mro__)
            if hasattr(hook_cls, "__hooks__")
            for hook in hook_cls.__hooks__.get(hook_name, [])
        ]

        for hook in hooks:
            try:
                hook(target, *args, **kwargs)
            except Exception as err:
                if hook.__hook__.can_fail:
                    logging.warn(f"{hook_name} has failed: {err}")
                else:
                    raise TriggerError(hook_name, err)
