import os
import sqlalchemy
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Iterable, Dict, List
from enum import Enum
import re

from meltano.core.utils import (
    find_named,
    setting_env,
    NotFound,
    truthy,
    flatten,
    set_at_path,
    pop_at_path,
)
from .setting_definition import SettingDefinition
from .setting import Setting
from .plugin_discovery_service import PluginDiscoveryService
from .config_service import ConfigService
from .error import Error


class SettingMissingError(Error):
    """Occurs when a setting is missing."""

    def __init__(self, name: str):
        super().__init__(f"Cannot find setting {name}")


class SettingValueSource(str, Enum):
    CONFIG_OVERRIDE = "config_override"  # 0
    ENV = "env"  # 1
    MELTANO_YML = "meltano_yml"  # 2
    DB = "db"  # 3
    DEFAULT = "default"  # 4

    @property
    def label(self):
        labels = {
            self.CONFIG_OVERRIDE: "config override",
            self.ENV: "environment",
            self.MELTANO_YML: "meltano.yml",
            self.DB: "system database",
            self.DEFAULT: "default",
        }
        return labels[self]


class SettingValueStore(str, Enum):
    MELTANO_YML = "meltano_yml"
    DB = "db"


# sentinel value to use to prevent leaking sensitive data
REDACTED_VALUE = "(redacted)"


class SettingsService(ABC):
    def __init__(
        self,
        project,
        plugin_discovery_service: PluginDiscoveryService = None,
        config_service: ConfigService = None,
        show_hidden=True,
        env_override={},
        config_override={},
    ):
        self.project = project

        self.discovery_service = plugin_discovery_service or PluginDiscoveryService(
            project
        )
        self.config_service = config_service or ConfigService(project)

        self.show_hidden = show_hidden

        self.env_override = env_override
        self.config_override = config_override

        self._env = None

    @property
    @abstractmethod
    def _env_namespace(self) -> str:
        pass

    @property
    @abstractmethod
    def _db_namespace(self) -> str:
        pass

    @property
    @abstractmethod
    def _definitions(self) -> List[SettingDefinition]:
        pass

    @property
    @abstractmethod
    def _current_config(self) -> Dict:
        pass

    @abstractmethod
    def _update_config(self):
        pass

    @property
    def flat_current_config(self):
        return flatten(self._current_config, "dot")

    @property
    def env(self):
        if not self._env:
            self._env = {**os.environ, **self.env_override}

        return self._env

    @classmethod
    def is_kind_redacted(cls, kind) -> bool:
        return kind in ("password", "oauth")

    @classmethod
    def unredact(cls, values: dict) -> Dict:
        """
        Removes any redacted values in a dictionary.
        """

        return {k: v for k, v in values.items() if v != REDACTED_VALUE}

    def with_env_override(self, env_override):
        return self.__class__(
            *self._args,
            **self._kwargs,
            env_override={**self.env_override, **env_override},
            config_override=self.config_override,
        )

    def with_config_override(self, config_override):
        return self.__class__(
            *self._args,
            **self._kwargs,
            env_override=self.env_override,
            config_override={**self.config_override, **config_override},
        )

    def config_with_metadata(self, sources: List[SettingValueSource] = None, **kwargs):
        config = {}
        for setting in self.definitions():
            value, source = self.get_value(setting.name, **kwargs)
            if sources and source not in sources:
                logging.debug(f"Setting {setting.name} is not in sources: {sources}.")
                continue

            config[setting.name] = {
                "value": value,
                "source": source,
                "setting": setting,
            }

        return config

    def as_config(self, *args, **kwargs) -> Dict:
        full_config = self.config_with_metadata(*args, **kwargs)

        return {key: config["value"] for key, config in full_config.items()}

    def as_env(self, *args, **kwargs) -> Dict[str, str]:
        full_config = self.config_with_metadata(*args, **kwargs)

        return {
            self.setting_env(config["setting"]): str(config["value"])
            for key, config in full_config.items()
            if config["value"] is not None
        }

    def get_value(self, name: str, redacted=False, session=None):
        try:
            setting_def = self.find_setting(name)
        except SettingMissingError:
            setting_def = None

        def config_override_getter():
            try:
                return self.config_override[name]
            except KeyError:
                return None

        def env_getter():
            if not setting_def:
                return None

            env_key = self.setting_env(setting_def)

            try:
                return self.env[env_key]
            except KeyError:
                return None
            else:
                logging.debug(
                    f"Found ENV variable {env_key} for {self._env_namespace}:{name}"
                )

        def meltano_yml_getter():
            try:
                value = self.flat_current_config[name]
                return self.expand_env_vars(value)
            except KeyError:
                return None

        def db_getter():
            if not session:
                return None

            try:
                return (
                    session.query(Setting)
                    .filter_by(namespace=self._db_namespace, name=name, enabled=True)
                    .one()
                    .value
                )
            except sqlalchemy.orm.exc.NoResultFound:
                return None

        def default_getter():
            if not setting_def:
                return None
            return self.expand_env_vars(setting_def.value)

        config_getters = {
            SettingValueSource.CONFIG_OVERRIDE: config_override_getter,
            SettingValueSource.ENV: env_getter,
            SettingValueSource.MELTANO_YML: meltano_yml_getter,
            SettingValueSource.DB: db_getter,
            SettingValueSource.DEFAULT: default_getter,
        }

        for source, getter in config_getters.items():
            value = getter()
            if value is not None:
                break

        if setting_def:
            value = self.cast_value(setting_def, value)

            # we don't want to leak secure informations
            # so we redact all `passwords`
            if redacted and value and self.is_kind_redacted(setting_def.kind):
                value = REDACTED_VALUE

        return value, source

    def set(
        self, path: List[str], value, store=SettingValueStore.MELTANO_YML, session=None
    ):
        if isinstance(path, str):
            path = [path]

        name = ".".join(path)

        if value == REDACTED_VALUE:
            return

        try:
            setting_def = self.find_setting(name)
        except SettingMissingError:
            setting_def = None

        if setting_def:
            value = self.cast_value(setting_def, value)

            env_key = self.setting_env(setting_def)

            if env_key in self.env:
                logging.warning(f"Setting `{name}` is currently set via ${env_key}.")

        def meltano_yml_setter():
            config = self._current_config

            if value is None:
                config.pop(name, None)
                pop_at_path(config, name, None)
                pop_at_path(config, path, None)
            else:
                if len(path) > 1:
                    config.pop(name, None)

                if name.split(".") != path:
                    pop_at_path(config, name, None)

                set_at_path(config, path, value)

            self._update_config()
            return True

        def db_setter():
            if not session:
                return None

            if value is None:
                session.query(Setting).filter_by(
                    namespace=self._db_namespace, name=name
                ).delete()
            else:
                setting = Setting(
                    namespace=self._db_namespace, name=name, value=value, enabled=True
                )
                session.merge(setting)

            session.commit()
            return True

        config_setters = {
            SettingValueStore.MELTANO_YML: meltano_yml_setter,
            SettingValueStore.DB: db_setter,
        }

        if not config_setters[store]():
            return

        return value

    def unset(self, path: List[str], **kwargs):
        return self.set(path, None, **kwargs)

    def reset(self, store=SettingValueStore.MELTANO_YML, session=None):
        def meltano_yml_resetter():
            self._current_config.clear()
            self._update_config()
            return True

        def db_resetter():
            if not session:
                return None

            session.query(Setting).filter_by(namespace=self._db_namespace).delete()
            session.commit()
            return True

        config_resetters = {
            SettingValueStore.MELTANO_YML: meltano_yml_resetter,
            SettingValueStore.DB: db_resetter,
        }

        if not config_resetters[store]():
            return False

        return True

    def definitions(self) -> Iterable[Dict]:
        definitions = deepcopy(self._definitions)
        definition_names = set(s.name for s in definitions)

        definitions.extend(
            (
                SettingDefinition.from_key_value(k, v)
                for k, v in self.flat_current_config.items()
                if k not in definition_names
            )
        )

        settings = []
        for setting in definitions:
            if setting.kind == "hidden" and not self.show_hidden:
                continue

            settings.append(setting)

        return settings

    def find_setting(self, name: str) -> SettingDefinition:
        try:
            return find_named(self.definitions(), name)
        except NotFound as err:
            raise SettingMissingError(name) from err

    def setting_env(self, setting_def):
        return setting_def.env or setting_env(self._env_namespace, setting_def.name)

    def cast_value(self, setting_def, value):
        if isinstance(value, str) and setting_def.kind == "boolean":
            value = truthy(value)

        return value

    def expand_env_vars(self, raw_value):
        if not isinstance(raw_value, str):
            return raw_value

        # find viable substitutions
        var_matcher = re.compile(
            """
            \$                 # starts with a '$'
            (?:                # either $VAR or ${VAR}
                {(\w+)}|(\w+)  # capture the variable name as group[0] or group[1]
            )
            """,
            re.VERBOSE,
        )

        def subst(match) -> str:
            try:
                # the variable can be in either group
                var = next(var for var in match.groups() if var)
                val = str(self.env[var])

                if not val:
                    logging.warning(f"Variable {var} is empty.")

                return val
            except KeyError as e:
                logging.warning(f"Variable {var} is missing from the environment.")
                return None

        fullmatch = re.fullmatch(var_matcher, raw_value)
        if fullmatch:
            # If the entire value is an env var reference, return None if it isn't set
            return subst(fullmatch)

        return re.sub(var_matcher, subst, raw_value)
