import os
import json
import logging
import socket
import argparse
import urllib.parse
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from yeelight import discover_bulbs, Bulb, PowerMode

app = FastAPI()
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

CACHE_FILE = 'cache.json'
NAMES_FILE = 'names.json'

def load_json(file_path: str) -> dict:
    # Load JSON file safely
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Read error {file_path}: {e}")
    return {}

def save_json(file_path: str, data: dict):
    # Save JSON file safely
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Write error {file_path}: {e}")

class DeviceNameRequest(BaseModel):
    bulb_id: str
    name: str

@app.get('/api/lights')
def get_lights():
    # Discover devices and append custom names
    known_devices = load_json(CACHE_FILE)
    custom_names = load_json(NAMES_FILE)
    discovered = discover_bulbs(timeout=2)
    discovered_ips = {b.get('ip'): b for b in discovered if b.get('ip')}
    
    all_ips = set(known_devices.keys()).union(set(discovered_ips.keys()))
    active_devices = {}
    socket.setdefaulttimeout(3)
    
    for ip in all_ips:
        try:
            device = Bulb(ip)
            props = device.get_properties(['bright', 'ct'])
            
            if props:
                capabilities = discovered_ips.get(ip, {}).get('capabilities', {})
                bulb_id = capabilities.get('id') or known_devices.get(ip, {}).get('id', 'Unknown')
                raw_model = capabilities.get('model') or known_devices.get(ip, {}).get('model', 'Unknown')
                
                active_devices[ip] = {
                    "ip": ip,
                    "id": bulb_id,
                    "model": raw_model,
                    "name": custom_names.get(bulb_id, "Unknown"),
                    "temperature_k": int(props.get('ct', 0)) if props.get('ct') else 0,
                    "brightness_pct": int(props.get('bright', 0)) if props.get('bright') else 0
                }
        except Exception:
            pass
            
    socket.setdefaulttimeout(10)
    if known_devices != active_devices:
        save_json(CACHE_FILE, active_devices)
        
    return {"status": "success", "data": list(active_devices.values())}

@app.get('/api/set')
def set_light(
    bulb_id: str = Query(..., alias="id"),
    temp: int = Query(None),
    brightness: float = Query(...)
):
    # Adjust lighting parameters
    known_devices = load_json(CACHE_FILE)
    target_ip = next((ip for ip, info in known_devices.items() if info.get('id') == bulb_id), None)

    if not target_ip:
        raise HTTPException(status_code=404, detail="Device not found")

    try:
        device = Bulb(target_ip)

        if brightness <= 0:
            device.turn_off()
        elif 0 < brightness < 1:
            props = device.get_properties(['active_mode'])
            supports_moonlight = props is not None and 'active_mode' in props

            if supports_moonlight:
                device.turn_on()
                device.set_power_mode(PowerMode.MOONLIGHT)
                moonlight_brightness = max(1, int(brightness * 100))
                device.set_brightness(moonlight_brightness, duration=1000)
            else:
                logging.info(f"Device {target_ip} does not support Moonlight. Powering off.")
                device.turn_off()
        else:
            device.turn_on()
            device.set_power_mode(PowerMode.NORMAL)
            if temp:
                device.set_color_temp(temp, duration=1000)
            device.set_brightness(int(brightness), duration=1000)

        return {"status": "success", "message": "Operation successful"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/api/level')
def get_or_set_level(
    bulb_id: str = Query(..., alias="id"),
    level: int = Query(None)
):
    # Locate device IP address via cached data
    known_devices = load_json(CACHE_FILE)
    target_ip = next((ip for ip, info in known_devices.items() if info.get('id') == bulb_id), None)
    
    if not target_ip:
        raise HTTPException(status_code=404, detail="Device not found")
        
    device = Bulb(target_ip)
    
    if level is None:
        # Retrieve current brightness and map to Matter logical range (0-254)
        try:
            props = device.get_properties(['bright'])
            current_bright = int(props.get('bright', 0)) if props and props.get('bright') else 0
            matter_level = int((current_bright / 100.0) * 254)
            return {"id": bulb_id, "level": matter_level}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    # Map Matter logical range back to hardware brightness percentage (0-100)
    try:
        if level <= 0:
            device.turn_off()
        else:
            device.turn_on()
            hardware_bright = int((level / 254.0) * 100)
            device.set_brightness(max(1, hardware_bright), duration=1000)
        return {"status": "success", "level": level}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/api/kelvin')
def get_or_set_kelvin(
    bulb_id: str = Query(..., alias="id"),
    kelvin: int = Query(None)
):
    # Locate device IP address via cached data
    known_devices = load_json(CACHE_FILE)
    target_ip = next((ip for ip, info in known_devices.items() if info.get('id') == bulb_id), None)
    
    if not target_ip:
        raise HTTPException(status_code=404, detail="Device not found")
        
    device = Bulb(target_ip)
    
    if kelvin is None:
        # Retrieve current color temperature directly from hardware
        try:
            props = device.get_properties(['ct'])
            current_kelvin = int(props.get('ct', 0)) if props and props.get('ct') else 0
            return {"id": bulb_id, "kelvin": current_kelvin}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    # Configure new color temperature
    try:
        device.turn_on()
        device.set_color_temp(kelvin, duration=1000)
        return {"status": "success", "kelvin": kelvin}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/api/mired')
def get_or_set_mired(
    bulb_id: str = Query(..., alias="id"),
    mired: int = Query(None)
):
    # Locate device IP address via cached data
    known_devices = load_json(CACHE_FILE)
    target_ip = next((ip for ip, info in known_devices.items() if info.get('id') == bulb_id), None)
    
    if not target_ip:
        raise HTTPException(status_code=404, detail="Device not found")
        
    device = Bulb(target_ip)
    
    if mired is None:
        # Retrieve current Kelvin and convert to Mired
        try:
            props = device.get_properties(['ct'])
            current_kelvin = int(props.get('ct', 0)) if props and props.get('ct') else 0
            current_mired = int(1000000 / current_kelvin) if current_kelvin > 0 else 0
            return {"id": bulb_id, "mired": current_mired}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    # Convert Mired input to Kelvin for hardware configuration
    try:
        kelvin = int(1000000 / mired) if mired > 0 else 4000
        device.turn_on()
        device.set_color_temp(kelvin, duration=1000)
        return {"status": "success", "mired": mired}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/api/name')
def set_device_name(request: DeviceNameRequest):
    # Store user defined device names
    names = load_json(NAMES_FILE)
    names[request.bulb_id] = request.name
    save_json(NAMES_FILE, names)
    return {"status": "success", "message": "Device name updated successfully"}

@app.get('/api/metadata')
def get_bridge_metadata(request: Request):
    # Dynamically detect host and port from the incoming HTTP request
    host = request.url.hostname
    port = request.url.port or 9800
    
    known_devices = load_json(CACHE_FILE)
    custom_names = load_json(NAMES_FILE)
    
    devices_metadata = []
    
    # Iterate over available units in the local network
    for ip, info in known_devices.items():
        bulb_id = info.get('id')
        if not bulb_id:
            continue
            
        name = custom_names.get(bulb_id, "Unknown")
        safe_id = urllib.parse.quote(bulb_id)
        node_identifier = bulb_id.replace(" ", "_").lower()
        
        device_config = {
            "node_id": f"yeelight_{node_identifier}",
            "name": name,
            "hardware_type": "color_temperature_light",
            "events": {
                "turn_on": {
                    "trigger": "on_off_cluster",
                    "script": f"import urllib.request\n# Execute GET request to set level to maximum (254)\nurllib.request.urlopen('http://{host}:{port}/api/level?id={safe_id}&level=254')"
                },
                "turn_off": {
                    "trigger": "on_off_cluster",
                    "script": f"import urllib.request\n# Execute GET request to turn off (0)\nurllib.request.urlopen('http://{host}:{port}/api/level?id={safe_id}&level=0')"
                },
                "set_level": {
                    "trigger": "level_control_cluster",
                    "script": f"import sys, urllib.request\n# Send integer level (0-254) directly to the API\nmatter_level = int(sys.argv[1]) if len(sys.argv) > 1 else 254\nurllib.request.urlopen(f'http://{host}:{port}/api/level?id={safe_id}&level={{matter_level}}')"
                },
                "read_level": {
                    "trigger": "level_control_cluster",
                    "script": f"import urllib.request, json\n# Retrieve integer level directly from the API\nres = urllib.request.urlopen('http://{host}:{port}/api/level?id={safe_id}')\ndata = json.loads(res.read().decode('utf-8'))\nprint(data.get('level', 0))"
                },
                "set_color_temperature": {
                    "trigger": "color_control_cluster",
                    "script": f"import sys, urllib.request\n# Set color temperature using Mired\nmired = int(sys.argv[1]) if len(sys.argv) > 1 else 250\nurllib.request.urlopen(f'http://{host}:{port}/api/mired?id={safe_id}&mired={{mired}}')"
                },
                "read_color_temperature": {
                    "trigger": "color_control_cluster",
                    "script": f"import urllib.request, json\n# Retrieve current color temperature in Mired\nres = urllib.request.urlopen('http://{host}:{port}/api/mired?id={safe_id}')\ndata = json.loads(res.read().decode('utf-8'))\nprint(data.get('mired', 0))"
                }
            }
        }
        devices_metadata.append(device_config)
        
    bridge_metadata = {
        "bridge": {
            "id": "yeelight_bridge_http",
            "type": "color_lighting_controller",
            "network_host": host,
            "network_port": port
        },
        "devices": devices_metadata
    }
    
    return bridge_metadata

def main():
    parser = argparse.ArgumentParser(description="Yeelight Local Control API")
    parser.add_argument('-p', '--port', type=int, default=9800, help="Web service port")
    args = parser.parse_args()
    
    logging.info(f"Starting service on port {args.port}")
    uvicorn.run(app, host='0.0.0.0', port=args.port)

if __name__ == '__main__':
    main()