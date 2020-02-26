import pytest

from meltano.core.db import project_engine
from meltano.api.models import db


class TestApp:
    @pytest.fixture
    def session(self):
        # disable the `session` fixture not to override
        # the `db.session`
        pass

    def test_core_registered(self, engine_sessionmaker, app):
        engine, _ = engine_sessionmaker

        # ensure both the API and the meltano.core
        # are on the same database
        assert engine.url == db.engine.url


class TestAppSMTPDefault:
    def test_config_smtp_default(self, app):
        defaults = {
            "MAIL_SERVER": "localhost",
            "MAIL_PORT": 1025,
            "MAIL_DEFAULT_SENDER": '"Meltano" <bot@meltano.com>',
            "MAIL_USE_TLS": False,
            "MAIL_USERNAME": None,
            "MAIL_PASSWORD": None,
            "MAIL_DEBUG": False,
        }

        for k, v in defaults.items():
            assert app.config[k] == v

        # ensure it is the defaults when not set
        assert defaults.items() <= app.config.items()


class TestAppSMTP:
    ENV = {
        "MAIL_SERVER": "smtp.localdomain",
        "MAIL_PORT": "1337",
        "MAIL_DEFAULT_SENDER": '"Doctor Who" <dr.who@localdomain.com>',
        "MAIL_USE_TLS": "1",
        "MAIL_USERNAME": "username",
        "MAIL_PASSWORD": "password",
        "MAIL_DEBUG": "0",
    }

    EXPECTED = {
        "MAIL_SERVER": "smtp.localdomain",
        "MAIL_PORT": 1337,
        "MAIL_DEFAULT_SENDER": '"Doctor Who" <dr.who@localdomain.com>',
        "MAIL_USE_TLS": True,
        "MAIL_USERNAME": "username",
        "MAIL_PASSWORD": "password",
        "MAIL_DEBUG": False,
    }

    @pytest.fixture
    def app(self, create_app, monkeypatch):
        # ensure the environment is properly loaded
        for env, value in self.ENV.items():
            monkeypatch.setenv(env, value)

        return create_app()

    def test_config_smtp(self, app):
        for k, v in self.EXPECTED.items():
            assert app.config[k] == v

        assert self.EXPECTED.items() <= app.config.items()
