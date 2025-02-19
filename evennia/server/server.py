"""
This module implements the main Evennia server process, the core of the game
engine.

This module should be started with the 'twistd' executable since it sets up all
the networking features.  (this is done automatically by
evennia/server/server_runner.py).

"""
import os
import sys
import time
import traceback

import django
from twisted.application import internet, service
from twisted.internet import defer, reactor
from twisted.internet.task import LoopingCall
from twisted.logger import globalLogPublisher
from twisted.web import static

django.setup()

import importlib

import evennia

evennia._init()

from django.conf import settings
from django.db import connection
from django.db.utils import OperationalError
from django.utils.translation import gettext as _
from evennia.accounts.models import AccountDB
from evennia.scripts.models import ScriptDB
from evennia.server.models import ServerConfig
from evennia.server.sessionhandler import SESSIONS
from evennia.utils import logger
from evennia.utils.utils import get_evennia_version, make_iter, mod_import

_SA = object.__setattr__

# a file with a flag telling the server to restart after shutdown or not.
SERVER_RESTART = os.path.join(settings.GAME_DIR, "server", "server.restart")

# modules containing hook methods called during start_stop
SERVER_STARTSTOP_MODULES = [
    mod_import(mod)
    for mod in make_iter(settings.AT_SERVER_STARTSTOP_MODULE)
    if isinstance(mod, str)
]

# modules containing plugin services
SERVER_SERVICES_PLUGIN_MODULES = make_iter(settings.SERVER_SERVICES_PLUGIN_MODULES)


# ------------------------------------------------------------
# Evennia Server settings
# ------------------------------------------------------------

SERVERNAME = settings.SERVERNAME
VERSION = get_evennia_version()

AMP_ENABLED = True
AMP_HOST = settings.AMP_HOST
AMP_PORT = settings.AMP_PORT
AMP_INTERFACE = settings.AMP_INTERFACE

WEBSERVER_PORTS = settings.WEBSERVER_PORTS
WEBSERVER_INTERFACES = settings.WEBSERVER_INTERFACES

GUEST_ENABLED = settings.GUEST_ENABLED

# server-channel mappings
WEBSERVER_ENABLED = settings.WEBSERVER_ENABLED and WEBSERVER_PORTS and WEBSERVER_INTERFACES
IRC_ENABLED = settings.IRC_ENABLED
RSS_ENABLED = settings.RSS_ENABLED
GRAPEVINE_ENABLED = settings.GRAPEVINE_ENABLED
WEBCLIENT_ENABLED = settings.WEBCLIENT_ENABLED
GAME_INDEX_ENABLED = settings.GAME_INDEX_ENABLED

INFO_DICT = {
    "servername": SERVERNAME,
    "version": VERSION,
    "amp": "",
    "errors": "",
    "info": "",
    "webserver": "",
    "irc_rss": "",
}

try:
    WEB_PLUGINS_MODULE = mod_import(settings.WEB_PLUGINS_MODULE)
except ImportError:
    WEB_PLUGINS_MODULE = None
    INFO_DICT["errors"] = (
        "WARNING: settings.WEB_PLUGINS_MODULE not found - "
        "copy 'evennia/game_template/server/conf/web_plugins.py to mygame/server/conf."
    )

# Maintenance function - this is called repeatedly by the server

_IDMAPPER_CACHE_MAXSIZE = settings.IDMAPPER_CACHE_MAXSIZE
_IDLE_TIMEOUT = settings.IDLE_TIMEOUT
_LAST_SERVER_TIME_SNAPSHOT = 0

_MAINTENANCE_COUNT = 0
_FLUSH_CACHE = None
_GAMETIME_MODULE = None
_OBJECTDB = None


def _server_maintenance():
    """
    This maintenance function handles repeated checks and updates that
    the server needs to do. It is called every minute.
    """
    global EVENNIA, _MAINTENANCE_COUNT, _FLUSH_CACHE, _GAMETIME_MODULE
    global _LAST_SERVER_TIME_SNAPSHOT
    global _OBJECTDB

    if not _OBJECTDB:
        from evennia.objects.models import ObjectDB as _OBJECTDB
    if not _GAMETIME_MODULE:
        from evennia.utils import gametime as _GAMETIME_MODULE
    if not _FLUSH_CACHE:
        from evennia.utils.idmapper.models import conditional_flush as _FLUSH_CACHE

    _MAINTENANCE_COUNT += 1

    now = time.time()
    if _MAINTENANCE_COUNT == 1:
        # first call after a reload
        _GAMETIME_MODULE.SERVER_START_TIME = now
        _GAMETIME_MODULE.SERVER_RUNTIME = ServerConfig.objects.conf("runtime", default=0.0)
        _LAST_SERVER_TIME_SNAPSHOT = now
    else:
        # adjust the runtime not with 60s but with the actual elapsed time
        # in case this may varies slightly from 60s.
        _GAMETIME_MODULE.SERVER_RUNTIME += now - _LAST_SERVER_TIME_SNAPSHOT
    _LAST_SERVER_TIME_SNAPSHOT = now

    # update game time and save it across reloads
    _GAMETIME_MODULE.SERVER_RUNTIME_LAST_UPDATED = now
    ServerConfig.objects.conf("runtime", _GAMETIME_MODULE.SERVER_RUNTIME)

    if _MAINTENANCE_COUNT % 5 == 0:
        # check cache size every 5 minutes
        _FLUSH_CACHE(_IDMAPPER_CACHE_MAXSIZE)
    if _MAINTENANCE_COUNT % (60 * 7) == 0:
        # drop database connection every 7 hrs to avoid default timeouts on MySQL
        # (see https://github.com/evennia/evennia/issues/1376)
        connection.close()

    # handle idle timeouts
    if _IDLE_TIMEOUT > 0:
        reason = _("idle timeout exceeded")
        to_disconnect = []
        for session in (
            sess for sess in SESSIONS.values() if (now - sess.cmd_last) > _IDLE_TIMEOUT
        ):
            if not session.account or not session.account.access(
                session.account, "noidletimeout", default=False
            ):
                to_disconnect.append(session)

        for session in to_disconnect:
            SESSIONS.disconnect(session, reason=reason)

    # run unpuppet hooks for objects that are marked as being puppeted,
    # but which lacks an account (indicates a broken unpuppet operation
    # such as a server crash)
    if _MAINTENANCE_COUNT > 1:
        unpuppet_count = 0
        for obj in _OBJECTDB.objects.get_by_tag(key="puppeted", category="account"):
            if not obj.has_account:
                obj.at_pre_unpuppet()
                obj.at_post_unpuppet(None, reason=_(" (connection lost)"))
                obj.tags.remove("puppeted", category="account")
                unpuppet_count += 1
        if unpuppet_count:
            logger.log_msg(f"Ran unpuppet-hooks for {unpuppet_count} link-dead puppets.")


# ------------------------------------------------------------
# Evennia Main Server object
# ------------------------------------------------------------


class Evennia:

    """
    The main Evennia server handler. This object sets up the database and
    tracks and interlinks all the twisted network services that make up
    evennia.

    """

    def __init__(self, application):
        """
        Setup the server.

        application - an instantiated Twisted application

        """
        sys.path.insert(1, ".")

        # create a store of services
        self.services = service.MultiService()
        self.services.setServiceParent(application)
        self.amp_protocol = None  # set by amp factory
        self.sessions = SESSIONS
        self.sessions.server = self
        self.process_id = os.getpid()

        # Database-specific startup optimizations.
        self.sqlite3_prep()

        self.start_time = time.time()

        # wrap the SIGINT handler to make sure we empty the threadpool
        # even when we reload and we have long-running requests in queue.
        # this is necessary over using Twisted's signal handler.
        # (see https://github.com/evennia/evennia/issues/1128)
        def _wrap_sigint_handler(*args):
            from twisted.internet.defer import Deferred

            if hasattr(self, "web_root"):
                d = self.web_root.empty_threadpool()
                d.addCallback(lambda _: self.shutdown("reload", _reactor_stopping=True))
            else:
                d = Deferred(lambda _: self.shutdown("reload", _reactor_stopping=True))
            d.addCallback(lambda _: reactor.stop())
            reactor.callLater(1, d.callback, None)

        reactor.sigInt = _wrap_sigint_handler

    # Server startup methods

    def sqlite3_prep(self):
        """
        Optimize some SQLite stuff at startup since we
        can't save it to the database.
        """
        if (
            ".".join(str(i) for i in django.VERSION) < "1.2"
            and settings.DATABASES.get("default", {}).get("ENGINE") == "sqlite3"
        ) or (
            hasattr(settings, "DATABASES")
            and settings.DATABASES.get("default", {}).get("ENGINE", None)
            == "django.db.backends.sqlite3"
        ):
            cursor = connection.cursor()
            cursor.execute("PRAGMA cache_size=10000")
            cursor.execute("PRAGMA synchronous=OFF")
            cursor.execute("PRAGMA count_changes=OFF")
            cursor.execute("PRAGMA temp_store=2")

    def update_defaults(self):
        """
        We make sure to store the most important object defaults here, so
        we can catch if they change and update them on-objects automatically.
        This allows for changing default cmdset locations and default
        typeclasses in the settings file and have them auto-update all
        already existing objects.

        """
        global INFO_DICT

        # setting names
        settings_names = (
            "CMDSET_CHARACTER",
            "CMDSET_ACCOUNT",
            "BASE_ACCOUNT_TYPECLASS",
            "BASE_OBJECT_TYPECLASS",
            "BASE_CHARACTER_TYPECLASS",
            "BASE_ROOM_TYPECLASS",
            "BASE_EXIT_TYPECLASS",
            "BASE_SCRIPT_TYPECLASS",
            "BASE_CHANNEL_TYPECLASS",
        )
        # get previous and current settings so they can be compared
        settings_compare = list(
            zip(
                [ServerConfig.objects.conf(name) for name in settings_names],
                [settings.__getattr__(name) for name in settings_names],
            )
        )
        mismatches = [
            i for i, tup in enumerate(settings_compare) if tup[0] and tup[1] and tup[0] != tup[1]
        ]
        if len(
            mismatches
        ):  # can't use any() since mismatches may be [0] which reads as False for any()
            # we have a changed default. Import relevant objects and
            # run the update
            from evennia.comms.models import ChannelDB
            from evennia.objects.models import ObjectDB

            # from evennia.accounts.models import AccountDB
            for i, prev, curr in (
                (i, tup[0], tup[1]) for i, tup in enumerate(settings_compare) if i in mismatches
            ):
                # update the database
                INFO_DICT[
                    "info"
                ] = " %s:\n '%s' changed to '%s'. Updating unchanged entries in database ..." % (
                    settings_names[i],
                    prev,
                    curr,
                )
                if i == 0:
                    ObjectDB.objects.filter(db_cmdset_storage__exact=prev).update(
                        db_cmdset_storage=curr
                    )
                if i == 1:
                    AccountDB.objects.filter(db_cmdset_storage__exact=prev).update(
                        db_cmdset_storage=curr
                    )
                if i == 2:
                    AccountDB.objects.filter(db_typeclass_path__exact=prev).update(
                        db_typeclass_path=curr
                    )
                if i in (3, 4, 5, 6):
                    ObjectDB.objects.filter(db_typeclass_path__exact=prev).update(
                        db_typeclass_path=curr
                    )
                if i == 7:
                    ScriptDB.objects.filter(db_typeclass_path__exact=prev).update(
                        db_typeclass_path=curr
                    )
                if i == 8:
                    ChannelDB.objects.filter(db_typeclass_path__exact=prev).update(
                        db_typeclass_path=curr
                    )
                # store the new default and clean caches
                ServerConfig.objects.conf(settings_names[i], curr)
                ObjectDB.flush_instance_cache()
                AccountDB.flush_instance_cache()
                ScriptDB.flush_instance_cache()
                ChannelDB.flush_instance_cache()
        # if this is the first start we might not have a "previous"
        # setup saved. Store it now.
        [
            ServerConfig.objects.conf(settings_names[i], tup[1])
            for i, tup in enumerate(settings_compare)
            if not tup[0]
        ]

    def run_initial_setup(self):
        """
        This is triggered by the amp protocol when the connection
        to the portal has been established.
        This attempts to run the initial_setup script of the server.
        It returns if this is not the first time the server starts.
        Once finished the last_initial_setup_step is set to 'done'

        """
        global INFO_DICT
        initial_setup = importlib.import_module(settings.INITIAL_SETUP_MODULE)
        last_initial_setup_step = ServerConfig.objects.conf("last_initial_setup_step")
        try:
            if not last_initial_setup_step:
                # None is only returned if the config does not exist,
                # i.e. this is an empty DB that needs populating.
                INFO_DICT["info"] = " Server started for the first time. Setting defaults."
                initial_setup.handle_setup()
            elif last_initial_setup_step not in ("done", -1):
                # last step crashed, so we weill resume from this step.
                # modules and setup will resume from this step, retrying
                # the last failed module. When all are finished, the step
                # is set to 'done' to show it does not need to be run again.
                INFO_DICT["info"] = " Resuming initial setup from step '{last}'.".format(
                    last=last_initial_setup_step
                )
                initial_setup.handle_setup(last_initial_setup_step)
        except Exception:
            # stop server if this happens.
            print(traceback.format_exc())
            print("Error in initial setup. Stopping Server + Portal.")
            self.sessions.portal_shutdown()

    def create_default_channels(self):
        """
        check so default channels exist on every restart, create if not.

        """

        from evennia.accounts.models import AccountDB
        from evennia.comms.models import ChannelDB
        from evennia.utils.create import create_channel

        superuser = AccountDB.objects.get(id=1)

        # mudinfo
        mudinfo_chan = settings.CHANNEL_MUDINFO
        if mudinfo_chan and not ChannelDB.objects.filter(db_key__iexact=mudinfo_chan["key"]):
            channel = create_channel(**mudinfo_chan)
            channel.connect(superuser)
        # connectinfo
        connectinfo_chan = settings.CHANNEL_CONNECTINFO
        if connectinfo_chan and not ChannelDB.objects.filter(
            db_key__iexact=connectinfo_chan["key"]
        ):
            channel = create_channel(**connectinfo_chan)
        # default channels
        for chan_info in settings.DEFAULT_CHANNELS:
            if not ChannelDB.objects.filter(db_key__iexact=chan_info["key"]):
                channel = create_channel(**chan_info)
                channel.connect(superuser)

    def run_init_hooks(self, mode):
        """
        Called by the amp client once receiving sync back from Portal

        Args:
            mode (str): One of shutdown, reload or reset

        """
        from evennia.typeclasses.models import TypedObject

        # start server time and maintenance task
        self.maintenance_task = LoopingCall(_server_maintenance)
        self.maintenance_task.start(60, now=True)  # call every minute

        # update eventual changed defaults
        self.update_defaults()

        # run at_init() on all cached entities on reconnect
        [
            [entity.at_init() for entity in typeclass_db.get_all_cached_instances()]
            for typeclass_db in TypedObject.__subclasses__()
        ]

        self.at_server_init()

        # call correct server hook based on start file value
        if mode == "reload":
            logger.log_msg("Server successfully reloaded.")
            self.at_server_reload_start()
        elif mode == "reset":
            # only run hook, don't purge sessions
            self.at_server_cold_start()
            logger.log_msg("Evennia Server successfully restarted in 'reset' mode.")
        elif mode == "shutdown":
            from evennia.objects.models import ObjectDB

            self.at_server_cold_start()
            # clear eventual lingering session storages
            ObjectDB.objects.clear_all_sessids()
            logger.log_msg("Evennia Server successfully started.")

        # always call this regardless of start type
        self.at_server_start()

    @defer.inlineCallbacks
    def shutdown(self, mode="reload", _reactor_stopping=False):
        """
        Shuts down the server from inside it.

        mode - sets the server restart mode.
           - 'reload' - server restarts, no "persistent" scripts
             are stopped, at_reload hooks called.
           - 'reset' - server restarts, non-persistent scripts stopped,
             at_shutdown hooks called but sessions will not
             be disconnected.
           - 'shutdown' - like reset, but server will not auto-restart.
        _reactor_stopping - this is set if server is stopped by a kill
           command OR this method was already called
           once - in both cases the reactor is
           dead/stopping already.
        """
        if _reactor_stopping and hasattr(self, "shutdown_complete"):
            # this means we have already passed through this method
            # once; we don't need to run the shutdown procedure again.
            defer.returnValue(None)

        from evennia.objects.models import ObjectDB
        from evennia.server.models import ServerConfig
        from evennia.utils import gametime as _GAMETIME_MODULE

        if mode == "reload":
            # call restart hooks
            ServerConfig.objects.conf("server_restart_mode", "reload")
            yield [o.at_server_reload() for o in ObjectDB.get_all_cached_instances()]
            yield [p.at_server_reload() for p in AccountDB.get_all_cached_instances()]
            yield [
                (s._pause_task(auto_pause=True) if s.is_active else None, s.at_server_reload())
                for s in ScriptDB.get_all_cached_instances()
                if s.id
            ]
            yield self.sessions.all_sessions_portal_sync()
            self.at_server_reload_stop()
            # only save monitor state on reload, not on shutdown/reset
            from evennia.scripts.monitorhandler import MONITOR_HANDLER

            MONITOR_HANDLER.save()
        else:
            if mode == "reset":
                # like shutdown but don't unset the is_connected flag and don't disconnect sessions
                yield [o.at_server_shutdown() for o in ObjectDB.get_all_cached_instances()]
                yield [p.at_server_shutdown() for p in AccountDB.get_all_cached_instances()]
                if self.amp_protocol:
                    yield self.sessions.all_sessions_portal_sync()
            else:  # shutdown
                yield [_SA(p, "is_connected", False) for p in AccountDB.get_all_cached_instances()]
                yield [o.at_server_shutdown() for o in ObjectDB.get_all_cached_instances()]
                yield [
                    (p.unpuppet_all(), p.at_server_shutdown())
                    for p in AccountDB.get_all_cached_instances()
                ]
                yield ObjectDB.objects.clear_all_sessids()
            yield [
                (s._pause_task(auto_pause=True), s.at_server_shutdown())
                for s in ScriptDB.get_all_cached_instances()
                if s.id and s.is_active
            ]
            ServerConfig.objects.conf("server_restart_mode", "reset")
            self.at_server_cold_stop()

        # tickerhandler state should always be saved.
        from evennia.scripts.tickerhandler import TICKER_HANDLER

        TICKER_HANDLER.save()

        # always called, also for a reload
        self.at_server_stop()

        if hasattr(self, "web_root"):  # not set very first start
            yield self.web_root.empty_threadpool()

        if not _reactor_stopping:
            # kill the server
            self.shutdown_complete = True
            reactor.callLater(1, reactor.stop)

        # we make sure the proper gametime is saved as late as possible
        ServerConfig.objects.conf("runtime", _GAMETIME_MODULE.runtime())

    def get_info_dict(self):
        """
        Return the server info, for display.

        """
        return INFO_DICT

    # server start/stop hooks

    def at_server_init(self):
        """
        This is called first when the server is starting, before any other hooks, regardless of how it's starting.
        """
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_init"):
                mod.at_server_init()

    def at_server_start(self):
        """
        This is called every time the server starts up, regardless of
        how it was shut down.

        """
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_start"):
                mod.at_server_start()

    def at_server_stop(self):
        """
        This is called just before a server is shut down, regardless
        of it is fore a reload, reset or shutdown.

        """
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_stop"):
                mod.at_server_stop()

    def at_server_reload_start(self):
        """
        This is called only when server starts back up after a reload.

        """
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_reload_start"):
                mod.at_server_reload_start()

    def at_post_portal_sync(self, mode):
        """
        This is called just after the portal has finished syncing back data to the server
        after reconnecting.

        Args:
            mode (str): One of 'reload', 'reset' or 'shutdown'.

        """

        from evennia.scripts.monitorhandler import MONITOR_HANDLER

        MONITOR_HANDLER.restore(mode == "reload")

        from evennia.scripts.tickerhandler import TICKER_HANDLER

        TICKER_HANDLER.restore(mode == "reload")

        # Un-pause all scripts, stop non-persistent timers
        ScriptDB.objects.update_scripts_after_server_start()

        # start the task handler
        from evennia.scripts.taskhandler import TASK_HANDLER

        TASK_HANDLER.load()
        TASK_HANDLER.create_delays()

        # create/update channels
        self.create_default_channels()

        # delete the temporary setting
        ServerConfig.objects.conf("server_restart_mode", delete=True)

    def at_server_reload_stop(self):
        """
        This is called only time the server stops before a reload.

        """
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_reload_stop"):
                mod.at_server_reload_stop()

    def at_server_cold_start(self):
        """
        This is called only when the server starts "cold", i.e. after a
        shutdown or a reset.

        """
        # We need to do this just in case the server was killed in a way where
        # the normal cleanup operations did not have time to run.
        from evennia.objects.models import ObjectDB

        ObjectDB.objects.clear_all_sessids()

        # Remove non-persistent scripts
        from evennia.scripts.models import ScriptDB

        for script in ScriptDB.objects.filter(db_persistent=False):
            script._stop_task()

        if GUEST_ENABLED:
            for guest in AccountDB.objects.all().filter(
                db_typeclass_path=settings.BASE_GUEST_TYPECLASS
            ):
                for character in guest.db._playable_characters:
                    if character:
                        character.delete()
                guest.delete()
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_cold_start"):
                mod.at_server_cold_start()

    def at_server_cold_stop(self):
        """
        This is called only when the server goes down due to a shutdown or reset.

        """
        for mod in SERVER_STARTSTOP_MODULES:
            if hasattr(mod, "at_server_cold_stop"):
                mod.at_server_cold_stop()


# ------------------------------------------------------------
#
# Start the Evennia game server and add all active services
#
# ------------------------------------------------------------


# Tell the system the server is starting up; some things are not available yet
try:
    ServerConfig.objects.conf("server_starting_mode", True)
except OperationalError:
    print("Server server_starting_mode couldn't be set - database not set up.")


# twistd requires us to define the variable 'application' so it knows
# what to execute from.
application = service.Application("Evennia")


if "--nodaemon" not in sys.argv and "test" not in sys.argv:
    # activate logging for interactive/testing mode
    logfile = logger.WeeklyLogFile(
        os.path.basename(settings.SERVER_LOG_FILE),
        os.path.dirname(settings.SERVER_LOG_FILE),
        day_rotation=settings.SERVER_LOG_DAY_ROTATION,
        max_size=settings.SERVER_LOG_MAX_SIZE,
    )
    globalLogPublisher.addObserver(logger.GetServerLogObserver()(logfile))


# The main evennia server program. This sets up the database
# and is where we store all the other services.
EVENNIA = Evennia(application)

if AMP_ENABLED:
    # The AMP protocol handles the communication between
    # the portal and the mud server. Only reason to ever deactivate
    # it would be during testing and debugging.

    ifacestr = ""
    if AMP_INTERFACE != "127.0.0.1":
        ifacestr = "-%s" % AMP_INTERFACE

    INFO_DICT["amp"] = "amp %s: %s" % (ifacestr, AMP_PORT)

    from evennia.server import amp_client

    factory = amp_client.AMPClientFactory(EVENNIA)
    amp_service = internet.TCPClient(AMP_HOST, AMP_PORT, factory)
    amp_service.setName("ServerAMPClient")
    EVENNIA.services.addService(amp_service)

if WEBSERVER_ENABLED:
    # Start a django-compatible webserver.

    from evennia.server.webserver import (
        DjangoWebRoot,
        LockableThreadPool,
        PrivateStaticRoot,
        Website,
        WSGIWebServer,
    )

    # start a thread pool and define the root url (/) as a wsgi resource
    # recognized by Django
    threads = LockableThreadPool(
        minthreads=max(1, settings.WEBSERVER_THREADPOOL_LIMITS[0]),
        maxthreads=max(1, settings.WEBSERVER_THREADPOOL_LIMITS[1]),
    )

    web_root = DjangoWebRoot(threads)
    # point our media resources to url /media
    web_root.putChild(b"media", PrivateStaticRoot(settings.MEDIA_ROOT))
    # point our static resources to url /static
    web_root.putChild(b"static", PrivateStaticRoot(settings.STATIC_ROOT))
    EVENNIA.web_root = web_root

    if WEB_PLUGINS_MODULE:
        # custom overloads
        web_root = WEB_PLUGINS_MODULE.at_webserver_root_creation(web_root)

    web_site = Website(web_root, logPath=settings.HTTP_LOG_FILE)
    web_site.is_portal = False

    INFO_DICT["webserver"] = ""
    for proxyport, serverport in WEBSERVER_PORTS:
        # create the webserver (we only need the port for this)
        webserver = WSGIWebServer(threads, serverport, web_site, interface="127.0.0.1")
        webserver.setName("EvenniaWebServer%s" % serverport)
        EVENNIA.services.addService(webserver)

        INFO_DICT["webserver"] += "webserver: %s" % serverport

ENABLED = []
if IRC_ENABLED:
    # IRC channel connections
    ENABLED.append("irc")

if RSS_ENABLED:
    # RSS feed channel connections
    ENABLED.append("rss")

if GRAPEVINE_ENABLED:
    # Grapevine channel connections
    ENABLED.append("grapevine")

if GAME_INDEX_ENABLED:
    from evennia.server.game_index_client.service import EvenniaGameIndexService

    egi_service = EvenniaGameIndexService()
    EVENNIA.services.addService(egi_service)

if ENABLED:
    INFO_DICT["irc_rss"] = ", ".join(ENABLED) + " enabled."

for plugin_module in SERVER_SERVICES_PLUGIN_MODULES:
    # external plugin protocols - load here
    plugin_module = mod_import(plugin_module)
    if plugin_module:
        plugin_module.start_plugin_services(EVENNIA)
    else:
        print(f"Could not load plugin module {plugin_module}")

# clear server startup mode
try:
    ServerConfig.objects.conf("server_starting_mode", delete=True)
except OperationalError:
    print("Server server_starting_mode couldn't unset - db not set up.")
