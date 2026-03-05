import os
import json
import logging
import socket
import argparse
import uvicorn
from fastapi import FastAPI, HTTPException, Query
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

@app.post('/api/name')
def set_device_name(request: DeviceNameRequest):
    # Store user defined device names
    names = load_json(NAMES_FILE)
    names[request.bulb_id] = request.name
    save_json(NAMES_FILE, names)
    return {"status": "success", "message": "Device name updated successfully"}

def main():
    parser = argparse.ArgumentParser(description="Yeelight Local Control API")
    parser.add_argument('-p', '--port', type=int, default=9800, help="Web service port")
    args = parser.parse_args()
    
    logging.info(f"Starting service on port {args.port}")
    uvicorn.run(app, host='0.0.0.0', port=args.port)

if __name__ == '__main__':
    main()