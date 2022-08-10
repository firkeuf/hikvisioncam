from datetime import datetime, timedelta
from aiohttp import web

import pytz
import httpx
import xml.etree.ElementTree as ET

from homeassistant.components.http import HomeAssistantView


class APIHikvisionCamView(HomeAssistantView):
    """View to handle Services requests."""

    url = "/api/hikvisioncam"
    name = "api:hikvision"

    #

    async def get(self, request):
        """Get registered services."""
        #services = await async_services_json(request.app["hass"])
        print(request)
        print(dir(request))
        body = await request.text()
        print(body)
        services = {'aaa': 'bbb'}
        return self.json(body)

    def xml_search(self, timezone, native_time_string, delta=5):
        native_time = datetime.strptime(native_time_string, '%Y-%m-%dT%H:%M:%S.%f')
        last_tripped_time = pytz.timezone(timezone).localize(native_time, is_dst=None)
        start_time = last_tripped_time - timedelta(seconds=delta)
        end_time = last_tripped_time
        return f'<?xml version="1.0" encoding="utf-8"?><CMSearchDescription><searchID>1</searchID><trackIDList><trackID>101</trackID></trackIDList><timeSpanList><timeSpan><startTime>{start_time.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</startTime><endTime>{end_time.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</endTime></timeSpan></timeSpanList><maxResults>2</maxResults><searchResultPostion>0</searchResultPostion><metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList></CMSearchDescription>'

    async def post(self, request):
        data = await request.json()
        hass = request.app["hass"]
        print(data)
        friendly_name = data.get('friendly_name').split()[0]
        last_tripped_time = data.get('last_tripped_time')

        xml_search = self.xml_search(hass.config.time_zone, last_tripped_time)

        cam_list = hass.data['binary_sensor'].config['binary_sensor']
        url_data = get_url(cam_list, friendly_name)
        print(url_data)
        host = url_data.get('host')
        username = url_data.get('username')
        password = url_data.get('password')

        #data = '<?xml version="1.0" encoding="utf-8"?><CMSearchDescription><searchID>1</searchID><trackIDList><trackID>101</trackID></trackIDList><timeSpanList><timeSpan><startTime>2022-07-22T15:09:15Z</startTime><endTime>2022-07-22T15:09:20Z</endTime></timeSpan></timeSpanList><maxResults>2500</maxResults><searchResultPostion>0</searchResultPostion><metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList></CMSearchDescription>'
        url_search = f'http://{host}/ISAPI/ContentMgmt/search'
        auth = httpx.DigestAuth(username, password)
        async with httpx.AsyncClient() as client:
            r = await client.post(url_search, data=xml_search, auth=auth)
        print(r)
        xml_string = r.text
        print(xml_string)
        namespace = '{http://www.hikvision.com/ver20/XMLSchema}'
        root = ET.fromstring(xml_string)
        download_name_list = root.findall(f'{namespace}matchList/{namespace}searchMatchItem/{namespace}mediaSegmentDescriptor/{namespace}playbackURI')
        try:
            download_name = download_name_list[-1].text.replace('&', '&amp;')
        except Exception as e:
            return e
        #download_name = 'rtsp://10.10.0.12/Streaming/tracks/101?starttime=2022-08-09T02:44:58Z&amp;endtime=2022-08-09T02:45:22Z&amp;name=ch01_00000000319000413&amp;size=15181692'

        xml_download = f'<downloadRequest><playbackURI>{download_name}</playbackURI></downloadRequest>'
        url_download = f'http://{host}/ISAPI/ContentMgmt/download'
        print('xml_download', xml_download)

        #async with httpx.AsyncClient() as client:
        #    r = await client.post(url_download, data=xml_download, auth=auth)

        client = httpx.AsyncClient()
        resp = web.StreamResponse()
        resp.headers['Content-Type'] = 'video/mp4'
        async with client.stream('POST', url_download, data=xml_download, auth=auth) as response:
            await resp.prepare(request)
            async for chunk in response.aiter_raw():
                await resp.write(chunk)
            await resp.write_eof()
            return resp

        #print(r)
        #services = [friendly_name, start_time, end_time, rtsp_url]
        #print(len(r.content))
        #print(r)
        #return r.stream


def get_url(cam_list, cam_name):
    for item in cam_list:
        if item.get('name') == cam_name:
            return item
    return {}

#class APIDomainServicesView(HomeAssistantView):
#    """View to handle DomainServices requests."""
#
#    url = "/api/services/{domain}/{service}"
#    name = "api:domain-services"
#
#    async def post(self, request, domain, service):
#        """Call a service.
#
#        Returns a list of changed states.
#        """
#        hass: ha.HomeAssistant = request.app["hass"]
#        body = await request.text()
#        try:
#            data = json_loads(body) if body else None
#        except ValueError:
#            return self.json_message(
#                "Data should be valid JSON.", HTTPStatus.BAD_REQUEST
#            )
#
#        context = self.context(request)
#
#        try:
#            await hass.services.async_call(
#                domain, service, data, blocking=True, context=context
#            )
#        except (vol.Invalid, ServiceNotFound) as ex:
#            raise HTTPBadRequest() from ex
#
#        changed_states = []
#
#        for state in hass.states.async_all():
#            if state.context is context:
#                changed_states.append(state)
#
#        return self.json(changed_states)
