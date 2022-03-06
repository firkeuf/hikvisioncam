"""Support for Hikvision event stream events represented as binary sensors."""
from __future__ import annotations

from datetime import timedelta
import logging
import time

# from pyhik.hikvision import HikCamera
from .utils import HikCamera, box_normalization, REGION_IDS, REGION_SENSORS
import voluptuous as vol

from homeassistant.components.binary_sensor import (
    PLATFORM_SCHEMA,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import (
    ATTR_LAST_TRIP_TIME,
    CONF_CUSTOMIZE,
    CONF_DELAY,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
    CONF_FILE_PATH,
    CONF_REGION,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import track_point_in_utc_time
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util.dt import utcnow

_LOGGER = logging.getLogger(__name__)

CONF_IGNORED = "ignored"

DEFAULT_PORT = 80
DEFAULT_IGNORED = False
DEFAULT_DELAY = 0

ATTR_DELAY = "delay"

DEVICE_CLASS_MAP = {
    "Motion": BinarySensorDeviceClass.MOTION,
    "Line Crossing": BinarySensorDeviceClass.MOTION,
    "Field Detection": BinarySensorDeviceClass.MOTION,
    "Video Loss": None,
    "Tamper Detection": BinarySensorDeviceClass.MOTION,
    "Shelter Alarm": None,
    "Disk Full": None,
    "Disk Error": None,
    "Net Interface Broken": BinarySensorDeviceClass.CONNECTIVITY,
    "IP Conflict": BinarySensorDeviceClass.CONNECTIVITY,
    "Illegal Access": None,
    "Video Mismatch": None,
    "Bad Video": None,
    "PIR Alarm": BinarySensorDeviceClass.MOTION,
    "Face Detection": BinarySensorDeviceClass.MOTION,
    "Scene Change Detection": BinarySensorDeviceClass.MOTION,
    "I/O": None,
    "Unattended Baggage": BinarySensorDeviceClass.MOTION,
    "Attended Baggage": BinarySensorDeviceClass.MOTION,
    "Recording Failure": None,
    "Exiting Region": BinarySensorDeviceClass.MOTION,
    "Entering Region": BinarySensorDeviceClass.MOTION,
}

CUSTOMIZE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_IGNORED, default=DEFAULT_IGNORED): cv.boolean,
        vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.positive_int,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_SSL, default=False): cv.boolean,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_CUSTOMIZE, default={}): vol.Schema(
            {cv.string: CUSTOMIZE_SCHEMA}
        ),
    }
)


def setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Hikvision binary sensor devices."""
    name = config.get(CONF_NAME)
    host = config[CONF_HOST]
    port = config[CONF_PORT]
    username = config[CONF_USERNAME]
    password = config[CONF_PASSWORD]

    customize = config[CONF_CUSTOMIZE]

    protocol = "https" if config[CONF_SSL] else "http"

    url = f"{protocol}://{host}"

    data = HikvisionData(hass, url, port, name, username, password)

    if data.sensors is None:
        _LOGGER.error("Hikvision event stream has no data, unable to set up")
        return

    entities = []

    for sensor, channel_list in data.sensors.items():
        for channel in channel_list:
            # Build sensor name, then parse customize config.
            if data.type == "NVR":
                sensor_name = f"{sensor.replace(' ', '_')}_{channel[1]}"
            else:
                sensor_name = sensor.replace(" ", "_")

            custom = customize.get(sensor_name.lower(), {})
            ignore = custom.get(CONF_IGNORED)
            delay = custom.get(CONF_DELAY)

            _LOGGER.debug(
                "Entity: %s - %s, Options - Ignore: %s, Delay: %s",
                data.name,
                sensor_name,
                ignore,
                delay,
            )
            if not ignore:
                entities.append(
                    HikvisionBinarySensor(hass, sensor, channel[1], data, delay)
                )
            if sensor in REGION_SENSORS:
                for region in REGION_IDS:
                    entities.append(
                        HikvisionBinarySensor(hass, sensor, channel[1], data, delay, region)
                    )
    add_entities(entities)


class HikvisionData:
    """Hikvision device event stream object."""

    def __init__(self, hass, url, port, name, username, password):
        """Initialize the data object."""
        self._url = url
        self._port = port
        self._name = name
        self._username = username
        self._password = password

        # Establish camera
        self.camdata = HikCamera(self._url, self._port, self._username, self._password)

        if self._name is None:
            self._name = self.camdata.get_name

        hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, self.stop_hik)
        hass.bus.listen_once(EVENT_HOMEASSISTANT_START, self.start_hik)

    def stop_hik(self, event):
        """Shutdown Hikvision subscriptions and subscription thread on exit."""
        self.camdata.disconnect()

    def start_hik(self, event):
        """Start Hikvision event stream thread."""
        self.camdata.start_stream()

    @property
    def sensors(self):
        """Return list of available sensors and their states."""
        return self.camdata.current_event_states

    @property
    def cam_id(self):
        """Return device id."""
        return self.camdata.get_id

    @property
    def name(self):
        """Return device name."""
        return self._name

    @property
    def type(self):
        """Return device type."""
        return self.camdata.get_type

    def get_attributes(self, sensor, channel):
        """Return attribute list for sensor/channel."""
        return self.camdata.fetch_attributes(sensor, channel)


class HikvisionBinarySensor(BinarySensorEntity):
    """Representation of a Hikvision binary sensor."""

    def __init__(self, hass, sensor, channel, cam, delay, region=''):
        """Initialize the binary_sensor."""
        self._hass = hass
        self._cam = cam
        self._sensor = sensor
        self._channel = channel
        self._region = region
        _LOGGER.error(f'__INIT__ region {region} sensor {sensor} channel {channel}')

        if self._cam.type == "NVR":
            self._name = f"{self._cam.name} {sensor} {channel}"
        else:
            if region:
                self._name = f"{self._cam.name} {sensor} Region{region}"
            else:
                self._name = f"{self._cam.name} {sensor}"

        if region:
            self._id = f"{self._cam.cam_id}.{sensor}.{channel}.{region}"
        else:
            self._id = f"{self._cam.cam_id}.{sensor}.{channel}"

        self._state = False

        if delay is None:
            self._delay = 0
        else:
            self._delay = delay

        self._timer = None

        # Register callback function with pyHik
        self._cam.camdata.add_update_callback(self._update_callback, f"{self._cam.cam_id}.{sensor}.{channel}{region}")
        # self._cam.camdata.add_update_callback(self._update_callback, f"{self._cam.cam_id}.{sensor}.{channel}")

    def _sensor_state(self):
        """Extract sensor state."""
        return self._cam.get_attributes(self._sensor, self._channel)[0]

    def _sensor_last_update(self):
        """Extract sensor last update time."""
        return self._cam.get_attributes(self._sensor, self._channel)[3]

    def _sensor_region(self):
        """Extract sensor last update time."""
        try:
            region = int(self._cam.get_attributes(self._sensor, self._channel)[4])
        except:
            region = ''
        return region

    def _sensor_box(self):
        """Extract sensor last update time."""
        try:
            attr = self._cam.get_attributes(self._sensor, self._channel)
            _LOGGER.warning(f'_sensor_box {attr}')
            box = box_normalization(attr[5])
        except Exception as e:
            _LOGGER.warning(f'_sensor_box Except {e}')
            box = None
        return box

    def _sensor_last_tripped_time(self):
        """Extract sensor last update time."""
        try:
            attr = self._cam.get_attributes(self._sensor, self._channel)
            time_stamp = attr[3].timestamp()
        except Exception as e:
            _LOGGER.warning(f'_sensor_last_tripped_time Except {e}')
            return time.time()
        return time_stamp

    def _sensor_image_path(self, box, time_stamp):
        if not self.is_on:
            return ''
        if box:
            filename = f'/config/www/hikvision/image_{self.name}_{time_stamp}_{box[0]}_{box[1]}_{box[2]}_{box[3]}.jpg'
        else:
            filename = f'/config/www/hikvision/image_{self.name}_{time_stamp}_full.jpg'
        return filename

    @property
    def name(self):
        """Return the name of the Hikvision sensor."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._id

    @property
    def is_on(self):
        """Return true if sensor is on."""
        return self._state  #self._sensor_state()

    @property
    def device_class(self):
        """Return the class of this sensor, from DEVICE_CLASSES."""
        try:
            return DEVICE_CLASS_MAP[self._sensor]
        except KeyError:
            # Sensor must be unknown to us, add as generic
            return None

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        region = self._sensor_region()
        if self.is_on:
            box = self._sensor_box()
        else:
            box = None
        time_stamp = self._sensor_last_tripped_time()
        path = self._sensor_image_path(box, time_stamp)
        attr = {ATTR_LAST_TRIP_TIME: self._sensor_last_update(),
                CONF_REGION: region,
                'box': box,
                CONF_FILE_PATH: path,
                }

        if self._delay != 0:
            attr[ATTR_DELAY] = self._delay
        _LOGGER.warning(f'extra_state_attributes self._regions = {self._region} and region = {region}')
        if self._region == region and region:
            _LOGGER.warning(f'extra_state_attributes in IF box = {box} path {path}')
            self._cam.camdata.get_image(box, path)
        return attr

    def async_update(self):
        _LOGGER.warning(f'async update -------------------')
        pass

    def schedule_update_ha_state(self, force_refresh: bool = False, region='', estate='') -> None:
        region = self._sensor_region()
        _LOGGER.error(f'schedule_update_ha_state {self.name} region = {region} estate {estate}')
        if self._region == region or region == '':
            self._state = (estate == True)
            _LOGGER.error(f'schedule_update_ha_state {self.name} self._region = {self._region} region = {region}')
            super(HikvisionBinarySensor, self).schedule_update_ha_state()

    def _update_callback(self, msg, region='', estate=''):
        """Update the sensor's state, if needed."""
        _LOGGER.debug("Callback signal from: %s", msg)
        _LOGGER.error(f'_update_callback self._region = {self._region} Region = {region}')

        if self._delay > 0 and not self.is_on:
            # Set timer to wait until updating the state
            def _delay_update(now):
                """Timer callback for sensor update."""
                _LOGGER.warning(
                    "%s Called delayed (%ssec) update", self._name, self._delay
                )
                self.schedule_update_ha_state(False, region, estate)
                self._timer = None

            if self._timer is not None:
                self._timer()
                self._timer = None

            self._timer = track_point_in_utc_time(
                self._hass, _delay_update, utcnow() + timedelta(seconds=self._delay)
            )

        elif self._delay > 0 and self.is_on:
            # For delayed sensors kill any callbacks on true events and update
            if self._timer is not None:
                self._timer()
                self._timer = None

            self.schedule_update_ha_state(False, region, estate)

        else:
            self.schedule_update_ha_state(False, region, estate)
