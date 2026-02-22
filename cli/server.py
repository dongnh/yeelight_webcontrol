import os
import json
import logging
import socket
import argparse
from flask import Flask, jsonify, request
from yeelight import discover_bulbs, Bulb, PowerMode

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

CACHE_FILE = 'cache.txt'

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Cache read error: {e}")
    return {}

def save_cache(data):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Cache write error: {e}")

@app.route('/api/lights', methods=['GET'])
def get_lights():
    try:
        known_devices = load_cache()
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
                        "temperature_k": int(props.get('ct', 0)) if props.get('ct') else 0,
                        "brightness_pct": int(props.get('bright', 0)) if props.get('bright') else 0
                    }
            except Exception:
                pass
                
        socket.setdefaulttimeout(10)
        if known_devices != active_devices:
            save_cache(active_devices)
            
        return jsonify({"status": "success", "data": list(active_devices.values())}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/set', methods=['GET'])
def set_light():
    try:
        bulb_id = request.args.get('id')
        temp = request.args.get('temp', type=int)
        brightness = request.args.get('brightness', type=float)

        if not bulb_id or brightness is None:
            return jsonify({"status": "error", "message": "Missing parameters"}), 400

        known_devices = load_cache()
        target_ip = next((ip for ip, info in known_devices.items() if info.get('id') == bulb_id), None)

        if not target_ip:
            return jsonify({"status": "error", "message": "Device not found"}), 404

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

        return jsonify({"status": "success", "message": "Operation successful"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def main():
    parser = argparse.ArgumentParser(description="Yeelight Local Control API")
    parser.add_argument('-p', '--port', type=int, default=9800, help="Web service port")
    args = parser.parse_args()
    
    logging.info(f"Starting service on port {args.port}")
    app.run(host='0.0.0.0', port=args.port)

if __name__ == '__main__':
    main()
