import logging
import os
import signal
from dataclasses import dataclass
from enum import Enum
from queue import Empty
from queue import SimpleQueue
from typing import Optional
from uuid import uuid4

import yaml
from paho.mqtt import client
from paho.mqtt.client import MQTTMessage
from pixelblaze import Pixelblaze

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s.%(msecs)03d %(levelname)s %(name)-12s : \t%(message)s',
                    datefmt='%H:%M:%S')

LOG = logging.getLogger('bifrost')


class SignalHandler:
    """
    Utility class to make it easier to track whether signals have been raised. Defaults to handling
    SIGINT as that's what we use when exiting with CTRL-C etc. Use e.g. 'signals.SIGINT in h.signals'
    as a test to see whether there's been a specific signal received since this object was created.
    There are a few convenience properties to help with this.
    """

    def __init__(self, signals=None):
        signals_to_watch = signals or [signal.SIGINT]
        self.signals = set()

        def handler(sig, _frame):
            self.signals.add(sig)

        for signal_to_watch in signals_to_watch:
            signal.signal(signal_to_watch, handler)

    @property
    def sigint(self) -> bool:
        return signal.SIGINT in self.signals


class MQTTContext:
    """
    Context manager to handle an MQTT connection. Deals with subscribing and re-subscribing on reconnection, and
    closes the connection cleanly on context exit.
    """

    def __init__(self, client_name: str = None, clean_session: bool = True, protocol=client.MQTTv311,
                 transport: str = 'tcp', host: str = None, port: int = 1883, keepalive: int = 60, bind_address='',
                 subscriptions=None, user=None, password=None):
        """
        :param client_name:
            Client name, should be unique. If not specified, a UUID4 is generated. These can be re-used, but any
            existing connection with the same name will be closed. As connections are automatically re-connected this
            can lead to connect / disconnect loops if you're not careful.
        :param clean_session:
            Whether new sessions after reconnection should be clean, defaults to True
        :param protocol:
            Protocol to use, defaults to MQTT v 3.11
        :param transport:
            Transport, defaults to 'tcp'
        :param host:
            Host, no default value, but will pick up from MQTT_HOST environment value if not specified
        :param port:
            Port for MQTT broker, defaults to 1883, will pick up from MQTT_PORT environment value
        :param keepalive:
            Keepalive in seconds, defaults to 60, override with MQTT_KEEPALIVE
        :param bind_address:
            Bind address, defaults to '', override with MQTT_BIND_ADDRESS
        :param subscriptions:
            A list of topics to subscribe to, defaults to none. These will be re-subscribed on reconnection
            if necessary, so you should specify them here rather than attempting to do so within the context
        :param user:
            If needed, specify a user-name. Override with MQTT_USER, defaults to None to indicate no user auth
        :param password:
            If needed, specify a password. Override with MQTT_PASSWORD, defaults to None to indicate no auth
        """
        self.client_name = client_name or str(uuid4())
        self.clean_session = clean_session
        self.protocol = protocol
        self.transport = transport
        self.host = os.getenv('MQTT_HOST') or host
        assert host is not None, 'A broker host must be provided'
        self.port = int(os.getenv('MQTT_PORT') or port)
        self.keepalive = int(os.getenv('MQTT_KEEPALIVE') or keepalive)
        self.bind_address = os.getenv('MQTT_BIND_ADDRESS') or bind_address
        self.client: Optional[client.Client] = None
        self.subscriptions = subscriptions or []
        if not isinstance(self.subscriptions, list):
            self.subscriptions = [self.subscriptions]
        self.user = os.getenv('MQTT_USER') or user
        self.password = os.getenv('MQTT_PASSWORD') or password

    def __enter__(self) -> client.Client:
        """
        Creates and configures a new client, creating a callback to subscribe to the requested topic or topics on
        connection. This handles re-subscribing on reconnection events, which is why the old lighting code eventually
        fell out of sync. Also handles a logger to push log messages through the context's logging object. Starts the
        connection loop using loop_start(), this creates a new thread which will be stopped when the context is closed.

        :return:
            a Paho client object
        """
        LOG.info(f'starting MQTT client, host={self.host}, port={self.port}, user={self.user}')

        self.client = client.Client(self.client_name)
        self.client.username_pw_set(self.user, self.password)

        def on_log(_client, _userdata, _level, buff):
            LOG.debug(buff)

        def on_connect(c: client.Client, _userdata, _flags, rc):
            if rc == 0:
                c.subscribe([(topic, 0) for topic in self.subscriptions])
            else:
                LOG.warning(f'Unable to connect to {self.host}, returned code={rc}')

        self.client.on_log = on_log
        self.client.on_connect = on_connect
        self.client.connect(host=self.host, port=self.port, keepalive=self.keepalive)
        self.client.loop_start()
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.loop_stop()


def build_message_queue(c: client.Client) -> SimpleQueue:
    """
    Simple function to build a message queue, wrapping up button MQTT messages in HueButtonEvent instances
    """
    queue = SimpleQueue()

    def on_message(_client, _userdata, message):
        button_message = HueButtonEvent.from_message(m=message)
        if button_message is not None:
            queue.put_nowait(button_message)

    c.on_message = on_message
    return queue


class PixelBlazeLocator:

    def __init__(self):
        self.pb_map: dict[str, Pixelblaze] = {}

    def find_pixelblaze(self, name: str) -> Optional[Pixelblaze]:
        if name in self.pb_map:
            return self.pb_map[name]
        else:
            for pb in Pixelblaze.EnumerateDevices(timeout=0):
                pb_name = pb.getDeviceName()
                if pb_name not in self.pb_map or (pb_name in self.pb_map and self.pb_map[pb_name].connectionBroken):
                    self.pb_map[pb_name] = pb
            if name in self.pb_map:
                return self.pb_map[name]
            else:
                return None


LOCATOR = PixelBlazeLocator()


def find_pixelblaze(name: str) -> Optional[Pixelblaze]:
    """
    Find a pixelblaze by name, the corresponding pixelblaze object, or None if no matching
    device could be found
    """
    return LOCATOR.find_pixelblaze(name=name)


class HueInteractionType(Enum):
    CLICK = 2
    PRESS = 0
    HOLD = 1
    RELEASE = 3


@dataclass
class HueButtonEvent:
    switch: str
    button: int
    interaction: HueInteractionType

    @staticmethod
    def from_message(m: MQTTMessage) -> Optional['HueButtonEvent']:
        s = m.payload.decode(encoding='UTF8')
        topic = m.topic.split('/')
        if len(topic) == 3 and topic[0] == 'hue' and topic[2] == 'buttonevent':
            return HueButtonEvent(switch=topic[1],
                                  button=int(s[0]),
                                  interaction=HueInteractionType(int(s[3])))


@dataclass
class HueMapping:
    switch: str
    pixelblaze_name: str
    default_brightness: float = 0.1
    _pb: Pixelblaze = None

    @property
    def pb(self) -> Optional[Pixelblaze]:
        if self._pb is not None and self._pb.connected:
            return self._pb
        else:
            if self._pb is not None:
                pass
            LOG.debug(f'locating pixelblaze for name {self.pixelblaze_name}')
            self._pb = find_pixelblaze(self.pixelblaze_name)
            if self._pb is not None:
                LOG.debug(f'found pixelblaze {self._pb} for name {self.pixelblaze_name}')
            else:
                LOG.debug(f'unable to find pixelblaze for name {self.pixelblaze_name}')
            return self._pb

    def __str__(self):
        return f'hue[{self.switch}]->pb[{self.pixelblaze_name}]'


handler = SignalHandler()

with open('config/bifrost.yml', 'r') as file:
    config_file = yaml.safe_load(file)

config = [HueMapping(switch=i['hue'],
                     pixelblaze_name=i['pb'],
                     default_brightness=i['default_brightness']) for i in
          config_file['mappings']]

LOG.info(f'retrieved mappings: ' + ', '.join([str(mapping) for mapping in config]))

with MQTTContext(host=config_file['mqtt']['host'],
                 port=config_file['mqtt']['port'],
                 user=config_file['mqtt']['user'],
                 password=config_file['mqtt']['password'],
                 subscriptions='hue/+/buttonevent') as client:
    queue = build_message_queue(client)
    switch_to_mapping = {entry.switch: entry for entry in config}
    while not handler.sigint:
        try:
            message: HueButtonEvent = queue.get(block=True, timeout=0.1)
            if message.switch in switch_to_mapping:
                mapping: HueMapping = switch_to_mapping[message.switch]
                pb: Pixelblaze = mapping.pb
                if pb is not None:
                    brightness = pb.getBrightnessSlider()
                    if message.button == 1 and message.interaction == HueInteractionType.CLICK:
                        # Main 'I' button, cycle through patterns or restore default brightness
                        # if currently zero
                        if brightness == 0:
                            pb.setBrightnessSlider(mapping.default_brightness)
                        else:
                            pb.nextSequencer()
                    if message.button == 2 and (message.interaction == HueInteractionType.HOLD or
                                                message.interaction == HueInteractionType.CLICK):
                        # Hold or click the bright / dim buttons to increase / decrease brightness
                        pb.setBrightnessSlider(min(1.0, brightness + 0.1))
                    if message.button == 3 and (message.interaction == HueInteractionType.HOLD or
                                                message.interaction == HueInteractionType.CLICK):
                        pb.setBrightnessSlider(max(0.1, brightness - 0.1))
                    if message.button == 4 and message.interaction == HueInteractionType.CLICK:
                        # Click the 'O' button to set brightness to zero
                        pb.setBrightnessSlider(0)
        except Empty:
            pass
