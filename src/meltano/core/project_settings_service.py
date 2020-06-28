from meltano.core.settings_service import SettingsService


class ProjectSettingsService(SettingsService):
    @property
    def _env_namespace(self):
        return "meltano"

    @property
    def _db_namespace(self):
        return "meltano"

    @property
    def _definitions(self):
        return self.config_service.settings

    @property
    def _meltano_yml_config(self):
        return self.config_service.current_config

    def _update_meltano_yml_config(self):
        self.config_service.update_config()
