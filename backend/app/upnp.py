"""Ask the home router to forward a port to this computer (UPnP IGD), and take the mapping back off.

Only used when the user turns on "Ask my router to open the port". It finds the router on the local network
(SSDP), reads its description, and calls AddPortMapping / DeletePortMapping / GetExternalIPAddress. No library needed.
Many routers have UPnP switched off; then the user opens the port by hand, or uses the tunnel.
"""
from __future__ import annotations

import asyncio
import re
import socket
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

SSDP = ("239.255.255.250", 1900)
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")
SEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
          "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
DESCRIPTION = "KeiroLake"


class UPnPError(RuntimeError):
    pass


def _discover_sync(timeout: float = 3.0) -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.sendto(SEARCH, SSDP)
        found = []
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and m.group(1).decode() not in found:
                found.append(m.group(1).decode())
        return found
    finally:
        sock.close()


def parse_description(xml: bytes, base: str) -> tuple[str, str] | None:
    """(service type, absolute control URL) of the router's WAN connection service, from its description XML."""
    root = ET.fromstring(xml)
    for svc in root.iter():
        if not svc.tag.endswith("service"):
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in svc}
        if fields.get("serviceType") in SERVICES and fields.get("controlURL"):
            return fields["serviceType"], urljoin(base, fields["controlURL"])
    return None


def soap_body(service: str, action: str, args: dict) -> str:
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    return (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{service}">{inner}'
            f"</u:{action}></s:Body></s:Envelope>")


async def _gateway() -> tuple[str, str]:
    locations = await asyncio.to_thread(_discover_sync)
    async with httpx.AsyncClient(timeout=5) as client:
        for loc in locations:
            try:
                found = parse_description((await client.get(loc)).content, loc)
            except (httpx.HTTPError, ET.ParseError):
                continue
            if found:
                return found
    raise UPnPError("No router answered. UPnP may be switched off in your router's settings.")


async def _call(action: str, args: dict) -> str:
    service, control = await _gateway()
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(control, content=soap_body(service, action, args),
                                 headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
    if resp.status_code != 200:
        detail = re.search(r"<errorDescription>(.*?)</errorDescription>", resp.text)
        raise UPnPError(f"The router refused: {detail.group(1) if detail else resp.status_code}")
    return resp.text


def local_ip_for(router_host: str = "8.8.8.8") -> str:
    """The address this computer uses on the local network (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((router_host, 80))
        return s.getsockname()[0]
    finally:
        s.close()


async def add_mapping(port: int) -> str:
    """Forward external TCP `port` to this computer; returns the router's public address when it says."""
    await _call("AddPortMapping", {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP", "NewInternalPort": port,
                                   "NewInternalClient": local_ip_for(), "NewEnabled": 1, "NewPortMappingDescription": DESCRIPTION,
                                   "NewLeaseDuration": 0})
    return await external_ip()


async def remove_mapping(port: int) -> None:
    await _call("DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP"})


async def external_ip() -> str:
    try:
        text = await _call("GetExternalIPAddress", {})
    except UPnPError:
        return ""
    m = re.search(r"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", text)
    return m.group(1) if m else ""
