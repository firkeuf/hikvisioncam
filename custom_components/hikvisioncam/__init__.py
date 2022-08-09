from .api import APIHikvisionCamView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType



async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the API with the HTTP interface."""
    hass.http.register_view(APIHikvisionCamView)
#    hass.http.register_view(APIDomainServicesView)
    return True

