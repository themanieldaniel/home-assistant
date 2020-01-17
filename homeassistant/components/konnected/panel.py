"""Support for Konnected devices."""
import asyncio
import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_STATE,
    CONF_ACCESS_TOKEN,
    CONF_BINARY_SENSORS,
    CONF_DEVICES,
    CONF_HOST,
    CONF_ID,
    CONF_NAME,
    CONF_PIN,
    CONF_PORT,
    CONF_SENSORS,
    CONF_SWITCHES,
    CONF_TYPE,
    CONF_ZONE,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_ACTIVATION,
    CONF_API_HOST,
    CONF_BLINK,
    CONF_DHT_SENSORS,
    CONF_DISCOVERY,
    CONF_DS18B20_SENSORS,
    CONF_INVERSE,
    CONF_MOMENTARY,
    CONF_PAUSE,
    CONF_POLL_INTERVAL,
    CONF_REPEAT,
    DOMAIN,
    ENDPOINT_ROOT,
    SIGNAL_SENSOR_UPDATE,
    STATE_LOW,
    ZONE_TO_PIN,
)
from .errors import CannotConnect

_LOGGER = logging.getLogger(__name__)

KONN_MODEL = "Konnected"
KONN_MODEL_PRO = "Konnected Pro"

# Indicate how each unit is controlled (pin or zone)
KONN_API_VERSIONS = {
    KONN_MODEL: CONF_PIN,
    KONN_MODEL_PRO: CONF_ZONE,
}


class AlarmPanel:
    """A representation of a Konnected alarm panel."""

    def __init__(self, hass, config_entry):
        """Initialize the Konnected device."""
        self.hass = hass
        self.config_entry = config_entry
        self.config = (
            config_entry.data
        )  # a configuration.yaml device config contained in a device entry
        self.host = self.config.get(CONF_HOST)
        self.port = self.config.get(CONF_PORT)
        self.client = None
        self.status = None
        self.api_version = KONN_API_VERSIONS[KONN_MODEL]

    @property
    def device_id(self):
        """Device id is the MAC address as string with punctuation removed."""
        return self.config.get(CONF_ID)

    @property
    def stored_configuration(self):
        """Return the configuration stored in `hass.data` for this device."""
        return self.hass.data[DOMAIN][CONF_DEVICES].get(self.device_id)

    def format_zone(self, zone, other_items=None):
        """Get zone or pin based dict based on the client type."""
        payload = {
            self.api_version: zone
            if self.api_version == CONF_ZONE
            else ZONE_TO_PIN[zone]
        }
        payload.update(other_items or {})
        return payload

    async def async_connect(self):
        """Connect to and setup a Konnected device."""
        try:
            import konnected

            self.client = konnected.Client(
                host=self.host,
                port=str(self.port),
                websession=aiohttp_client.async_get_clientsession(self.hass),
            )
            self.status = await self.client.get_status()
            self.api_version = KONN_API_VERSIONS.get(
                self.status.get("model", KONN_MODEL), KONN_API_VERSIONS[KONN_MODEL]
            )
            _LOGGER.info(
                "Connected to new %s device", self.status.get("model", "Konnected")
            )
            _LOGGER.info(self.status)

            await self.async_update_initial_states()
            # brief delay to allow processing of recent status req
            await asyncio.sleep(0.1)
            await self.async_sync_device_config()

        except self.client.ClientError as err:
            _LOGGER.warning("Exception trying to connect to panel: %s", err)
            raise CannotConnect

        _LOGGER.info(
            "Set up Konnected device %s. Open http://%s:%s in a "
            "web browser to view device status.",
            self.device_id,
            self.host,
            self.port,
        )

        device_registry = await dr.async_get_registry(self.hass)

        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, self.status.get("mac"))},
            identifiers={(DOMAIN, self.device_id)},
            manufacturer="Konnected.io",
            name=self.config_entry.title,
            model=self.config_entry.title,
            sw_version=self.status.get("swVersion"),
        )

    async def update_switch(self, zone, state, momentary=None, times=None, pause=None):
        """Update the state of a switchable output."""
        try:
            if self.client:
                if self.api_version == CONF_ZONE:
                    return await self.client.put_zone(
                        zone, state, momentary, times, pause,
                    )

                # device endpoint uses pin numver instead of zone
                return await self.client.put_device(
                    ZONE_TO_PIN[zone], state, momentary, times, pause,
                )

        except self.client.ClientError as err:
            _LOGGER.warning("Exception trying to update panel: %s", err)

        raise CannotConnect

    async def async_save_data(self):
        """Save the device configuration to `hass.data`."""
        binary_sensors = {}
        for entity in self.config.get(CONF_BINARY_SENSORS) or []:
            zone = entity[CONF_ZONE]

            binary_sensors[zone] = {
                CONF_TYPE: entity[CONF_TYPE],
                CONF_NAME: entity.get(
                    CONF_NAME, "Konnected {} Zone {}".format(self.device_id[6:], zone)
                ),
                CONF_INVERSE: entity.get(CONF_INVERSE),
                ATTR_STATE: None,
            }
            _LOGGER.debug(
                "Set up binary_sensor %s (initial state: %s)",
                binary_sensors[zone].get("name"),
                binary_sensors[zone].get(ATTR_STATE),
            )

        actuators = []
        for entity in self.config.get(CONF_SWITCHES) or []:
            zone = entity[CONF_ZONE]

            act = {
                CONF_ZONE: zone,
                CONF_NAME: entity.get(
                    CONF_NAME,
                    "Konnected {} Actuator {}".format(self.device_id[6:], zone),
                ),
                ATTR_STATE: None,
                CONF_ACTIVATION: entity[CONF_ACTIVATION],
                CONF_MOMENTARY: entity.get(CONF_MOMENTARY),
                CONF_PAUSE: entity.get(CONF_PAUSE),
                CONF_REPEAT: entity.get(CONF_REPEAT),
            }
            actuators.append(act)
            _LOGGER.debug("Set up switch %s", act)

        sensors = []
        for entity in self.config.get(CONF_SENSORS) or []:
            zone = entity[CONF_ZONE]

            sensor = {
                CONF_ZONE: zone,
                CONF_NAME: entity.get(
                    CONF_NAME, "Konnected {} Sensor {}".format(self.device_id[6:], zone)
                ),
                CONF_TYPE: entity[CONF_TYPE],
                CONF_POLL_INTERVAL: entity.get(CONF_POLL_INTERVAL),
            }
            sensors.append(sensor)
            _LOGGER.debug(
                "Set up %s sensor %s (initial state: %s)",
                sensor.get(CONF_TYPE),
                sensor.get(CONF_NAME),
                sensor.get(ATTR_STATE),
            )

        device_data = {
            CONF_BINARY_SENSORS: binary_sensors,
            CONF_SENSORS: sensors,
            CONF_SWITCHES: actuators,
            CONF_BLINK: self.config.get(CONF_BLINK),
            CONF_DISCOVERY: self.config.get(CONF_DISCOVERY),
            CONF_HOST: self.host,
            CONF_PORT: self.port,
            "panel": self,
        }

        if CONF_DEVICES not in self.hass.data[DOMAIN]:
            self.hass.data[DOMAIN][CONF_DEVICES] = {}

        _LOGGER.debug(
            "Storing data in hass.data[%s][%s][%s]: %s",
            DOMAIN,
            CONF_DEVICES,
            self.device_id,
            device_data,
        )
        self.hass.data[DOMAIN][CONF_DEVICES][self.device_id] = device_data

    @callback
    def async_binary_sensor_configuration(self):
        """Return the configuration map for syncing binary sensors."""
        return [
            self.format_zone(p) for p in self.stored_configuration[CONF_BINARY_SENSORS]
        ]

    @callback
    def async_actuator_configuration(self):
        """Return the configuration map for syncing actuators."""
        return [
            self.format_zone(
                data[CONF_ZONE],
                {"trigger": (0 if data.get(CONF_ACTIVATION) in [0, STATE_LOW] else 1)},
            )
            for data in self.stored_configuration[CONF_SWITCHES]
        ]

    @callback
    def async_dht_sensor_configuration(self):
        """Return the configuration map for syncing DHT sensors."""
        return [
            self.format_zone(
                sensor[CONF_ZONE], {CONF_POLL_INTERVAL: sensor[CONF_POLL_INTERVAL]}
            )
            for sensor in self.stored_configuration[CONF_SENSORS]
            if sensor[CONF_TYPE] == "dht"
        ]

    @callback
    def async_ds18b20_sensor_configuration(self):
        """Return the configuration map for syncing DS18B20 sensors."""
        return [
            self.format_zone(sensor[CONF_ZONE])
            for sensor in self.stored_configuration[CONF_SENSORS]
            if sensor[CONF_TYPE] == "ds18b20"
        ]

    async def async_update_initial_states(self):
        """Update the initial state of each sensor from status poll."""
        for sensor_data in self.status.get("sensors"):
            sensor_config = self.stored_configuration[CONF_BINARY_SENSORS].get(
                sensor_data.get(CONF_ZONE, sensor_data.get(CONF_PIN)), {}
            )
            entity_id = sensor_config.get(ATTR_ENTITY_ID)

            state = bool(sensor_data.get(ATTR_STATE))
            if sensor_config.get(CONF_INVERSE):
                state = not state

            async_dispatcher_send(
                self.hass, SIGNAL_SENSOR_UPDATE.format(entity_id), state
            )

    @callback
    def async_desired_settings_payload(self):
        """Return a dict representing the desired device configuration."""
        desired_api_host = (
            self.hass.data[DOMAIN].get(CONF_API_HOST) or self.hass.config.api.base_url
        )
        desired_api_endpoint = desired_api_host + ENDPOINT_ROOT

        return {
            "sensors": self.async_binary_sensor_configuration(),
            "actuators": self.async_actuator_configuration(),
            "dht_sensors": self.async_dht_sensor_configuration(),
            "ds18b20_sensors": self.async_ds18b20_sensor_configuration(),
            "auth_token": self.hass.data[DOMAIN].get(
                CONF_ACCESS_TOKEN
            ),  # one common token to all devices
            "endpoint": desired_api_endpoint,
            "blink": self.config.get(CONF_BLINK),
            "discovery": self.config.get(CONF_DISCOVERY),
        }

    @callback
    def async_current_settings_payload(self):
        """Return a dict of configuration currently stored on the device."""
        settings = self.status["settings"]
        if not settings:
            settings = {}

        return {
            "sensors": [
                {self.api_version: s[self.api_version]}
                for s in self.status.get("sensors")
            ],
            "actuators": self.status.get("actuators"),
            "dht_sensors": self.status.get(CONF_DHT_SENSORS),
            "ds18b20_sensors": self.status.get(CONF_DS18B20_SENSORS),
            "auth_token": settings.get("token"),
            "endpoint": settings.get("endpoint"),
            "blink": settings.get(CONF_BLINK),
            "discovery": settings.get(CONF_DISCOVERY),
        }

    async def async_sync_device_config(self):
        """Sync the new zone configuration to the Konnected device if needed."""
        _LOGGER.debug(
            "Device %s settings payload: %s",
            self.device_id,
            self.async_desired_settings_payload(),
        )
        if (
            self.async_desired_settings_payload()
            != self.async_current_settings_payload()
        ):
            _LOGGER.info("pushing settings to device %s", self.device_id)
            await self.client.put_settings(**self.async_desired_settings_payload())


async def get_status(hass, host, port):
    """Get the status of a Konnected Panel."""
    import konnected

    client = konnected.Client(
        host, str(port), aiohttp_client.async_get_clientsession(hass)
    )
    try:
        return await client.get_status()

    except client.ClientError as err:
        _LOGGER.error("Exception trying to get panel status: %s", err)
        raise CannotConnect
