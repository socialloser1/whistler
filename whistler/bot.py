#! /usr/bin/env python
# -*- encoding: utf-8 -*-
# vim:fenc=utf-8:
#
# This code is heavly based on quinoa, which is:
#   (c) 2010 Kit La Touche

"""
The bot module
--------------

The bot module provide a set of classes to instance a basic bot, which
handle commands received in MUC stream and also user messages to, and
parse them trying to execute a valid command.

The :class:`WhistlerBot` is the main class used to start the bot, and is
designed to be extended when require. Let's an example:

.. code-block:: python

    from whistler.bot import WhistlerBot

    class MyBot(WhistlerBot):
        def cmd_ping(self, msg, args):
            return "pong"


The previous example create a ping/pong bot in a three lines. More complex
action can be used too.
"""

import os
import sys
import time
import random
import warnings

try:
    from functools import update_wrapper
except ImportError:
    # Old (pre 2.6) Python does not have it, provide a simple replacement
    def update_wrapper(wrapper, wrapped, *arg, **kw):
        wrapper.__name__ = wrapped.__name__
        wrapper.__doc__  = getattr(wrapped, "__doc__", None)
        return wrapper


from sleekxmpp.clientxmpp import ClientXMPP

from whistler.log import WhistlerLog
from whistler.job import WhistlerIdleJob

COMMAND_CHAR = "!"


def restricted(fun):
    """Decorator to restrict access to bot functionality.

    The restricted decorator is designed to work in commands, to check
    whether the user is in authorized users to perform an action. Example
    of usage:

    .. code-block:: python

      @restricted
      def cmd_ping(self, msg, args):
          return "pong"

    In this example ping command is only allowed to authenticated users.

    """
    def new(self, msg, args):
        user = msg["from"].bare
        if self.is_validuser(user):
            return fun(self, msg, args)
        else:
            self.log.warning("ignoring command %s, invalid user %s." % \
                            ( fun.__name__[4:], user ))
    return update_wrapper(new, fun)


class WhistlerConnectionError(Exception):
    """Exception which will be raised on bot connection error."""


class WhistlerBot(object):
    """Main Whistler bot class.

    The main WhistlerBot class handle the bot behaviour and perform subcall
    to specific command handler when a command is received in a configured
    MUC channel.

    """

    def __init__(self, jid, password, server=None, rooms=None,
            resource=None, log=None, users=None):
        """Initialize a Whistler bot.

        Create a new :class:`WhistlerBot` object, the :func:`__init__`
        receive the following parameters:

        :param `jid`: a valid JID atom to identify the bot user.
        :param `password`: a plaintext password to identify the bot user.
        :param `server`: a tuple in the form *server*, *port* which sepcify
            the host to connect to, if not provided the server then use the
            JID domain instead.
        :param `rooms`: a :class:`list` of rooms to be autojoin in.
        :param `resource`: the XMPP resource string, or autogenerated one if
            not provided.
        :param `log`: a :class:`WhistlerLog` to log bot messages to, or
            *stdout* if none is provided.
        :param `users`: a :class:`set` of valid JID as strings which
            identify master users.

        """
        self.jid = jid
        self.password = password
        self.server = server
        self.log = log or WhistlerLog()
        self.debug = False
        self._initial_users = users

        self.idle = None
        self.client = None

        self.resource = resource or self.__class__.__name__.lower() + \
                                    str(random.getrandbits(32))

        self.jid += "/" + self.resource
        self.rooms = set(rooms or [])

    @property
    def users(self):
        """Users in the bot roster (administrators)

        A property which return an iterator over users in bot roster, that is
        administrative users or valid users to admin the bot.

        """
        for jid in self.client.roster.iterkeys():
            if jid not in self.rooms and jid != self.jid:
                yield jid


    def send_to(self, who, data):
        """Send a chat message to any user.

        This function is designed to be called from user custom handle
        functions, using :fun:`register_handler`.

        :param `who`: The JID as string representation of the recipient.
        :param `data`: An string which contain the message to be set.

        """
        dest = xmpp.JID(who)
        self.client.send( xmpp.protocol.Message(dest, data, "chat") )


    def set_subject(self, room, subject):
        """Set a new subject on specified room."""

        if room in self.rooms.keys():
            dest = xmpp.JID(room)
            mesg = "Whistler set subject to: %s" % subject
            self.client.send( xmpp.protocol.Message(dest, mesg,
                              "groupchat", subject=subject) )


    def connect(self):
        """Perform a connection to the server.

        This function is designed to work internally, but calls to connect
        handlers when connection is sucessful.

        """
        if self.client:
            return self.client

        self.client = ClientXMPP(self.jid, self.password)

        # Install event handlers
        self.client.add_event_handler("groupchat_message", self.handle_muc_message)
        self.client.add_event_handler("session_start", self.handle_session_start)
        self.client.add_event_handler("message", self.handle_message)

        # Add plug-ins
        self.client.register_plugin("xep_0030") # Service Discovery
        self.client.register_plugin("xep_0004") # Data Forms
        self.client.register_plugin("xep_0060") # PubSub
        self.client.register_plugin("xep_0199") # XMPP Ping
        self.client.register_plugin("xep_0045") # Multi-User Chat

        if self.client.connect(self.server or ()):
            self.log.info("connected to %s, port %d" % self.server)
            self.client.start_tls()
            self.log.info("did STARTTLS successfully")
        else:
            raise WhistlerConnectionError(
                "unable to connect to %s using port %d" % self.server
            )

        return self.client


    def handle_session_start(self, event):
        self.client.get_roster()
        self.client.send_presence()

        # XXX Is the idle job still needed??
        self.idle = WhistlerIdleJob(self.client, 60)
        self.idle.start()

        [self.join_room(room) for room in self.rooms]
        for user in self._initial_users:
            self.register_user(user)


    def register_command(self, cmdname, cmdfun):
        """Register a new command.

        This function in intended to provide a way to add commands
        on-the-fly, when :class:`WhistlerBot` is alreay instanced.

        :param `cmdname`: a name to this command.
        :param `cmdfun`: a callback which can accept three arguments, which
            will be usd when command called.

        """
        setattr(self, "cmd_%s" % cmdname, cmdfun)


    def start(self):
        """Start bot operation.

        Connect to the XMPP server and start the bot, it will be serving
        requests until the stopping is requested, using :func:`stop`
        function.

        """
        if not self.connect():
            raise WhistlerConnectionError("unknown error")

        self.client.process(threaded=False)


    def stop(self):
        """Stop bot operation.

        Stop serving requests. This function also destroys the current
        connection, if existed.

        """
        self.disconnect()

        if self.idle:
            self.idle.stop()


    def is_validuser(self, jid):
        """Check for whether an user is valid.

        Check whether the specified user is registered as valid user in the
        bot, according to :func:`register_user` and :func:`unregister_user`
        functions.

        """
        return jid not in self.rooms and jid in self.client.roster


    def register_user(self, jid):
        """Register an user as valid user for the bot."""

        self.client.update_roster(jid, subscription="both")


    def unregister_user(self, jid):
        """Unregister an user as valid user for the bot."""

        self.client.update_roster(jid, subscription="remove")


    def handle_presence(self, client, message):
        """Handle the presence in XMPP server.

        This function is designed to work internally to bot, and handle the
        presence subscription XMPP message.

        """
        presence_type = message.getType()
        who = message.getFrom()

        if presence_type == "subscribe":

            if who not in self._initial_users:
                return

            self.client.send(xmpp.protocol.Presence(to=who, typ="subscribed"))
            self.client.send(xmpp.protocol.Presence(to=who, typ="subscribe"))

        if presence_type == "subscribed" and who in self._initial_users:
            self._initial_users.discard(who)


    def hande_muc_message(self, message):
        """ Handle any received group chat message.

        :param message: Message received from the MUC room.

        """
        body = message["body"]

        if not body or (body[0] != COMMAND_CHAR \
                and not body.startswith(self.resource + ", ") \
                and not body.startswith(self.resource + ": ")):
            # None to handle
            return

        if body[0] == COMMAND_CHAR:
            command_n = body.split()[0][1:]
            arguments = body.split()[1:]
        else:
            command_n = body.split()[1]
            arguments = body.split()[2:]

        command = getattr(self, "cmd_%s" % command_n, None)

        if command:
            self.log.info("muc command %s %r" % (command_n, arguments))
            result = command(message, arguments)
            if result is not None:
                message.reply(result).send()

    def handle_message(self, message):
        """Handle a received chat message."""

        if not message["body"]:
            return

        body = message["body"].split()

        command_n = body[0]
        arguments = body[1:]

        command = getattr(self, "cmd_%s" % command_n, None)

        if command:
            self.log.info("chat command %s %r" % (command_n, arguments))
            result = command(message, arguments)
            if result is not None:
                message.reply(result).send()


    def join_room(self, room, server, resource=None):
        """Join a Multi-User Chat (MUC) room.

        Make the bot join a MUC room. If a nick different from the resource
        name is to be used, it can be specified. This allows for several
        bots to be in the same room.

        """
        self.client.plugin["xep_0045"].joinMUC(room, resource or self.resource)


    def disconnect(self):
        """Disconnect from the server.

        Leave all rooms, sets bot presence to unavailable, and closes the
        connection to the server.

        """
        self.log.info("Shutting down the bot...")
        [self.leave_room(room) for room in self.rooms]
        self.client.disconnect()


    def leave_room(self, room, resource=None):
        """
        Perform an action to leave a room where currently the bot is in.

        :param `room`: the room name to leave.
        :param `resource`: the resource which leaves.

        """
        self.client.plugin["xep_0045"].leaveMUC(room, resource or self.resource)


if __name__ == "__main__":
    class TestBot(WhistlerBot):
        def cmd_echo(self, msg, args):
            return msg["body"]

        def cmd_list_rooms(self, msg, args):
            return ', '.join(self.client["xep_0045"].rooms.keys())

        def cmd_whoami(self, msg, args):
            return "You are %s" % msg["from"]

    try:
        b = TestBot('test@connectical.com',  'password',
                server = ("talk.google.com", 5223), resource = 'Bot')
        b.start()

    except KeyboardInterrupt:
        pass
    finally:
        b.stop()

