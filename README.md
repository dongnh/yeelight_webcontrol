# Yeelight Web Controller

Using WIFI communication to control a Yeelight-based home lighting system via a web interface, run on local machines PC/Mac.

## Prerequisites
- Python 3.12 or higher.

## Installation
Create a virtual environment, activate it, and install the `yeelight-web-controller` package using your preferred Python package manager.
```bash
python -m venv venv
source venv/bin/activate
pip install casambi-web-controller
```

## Execution
Run the executable command provided by the package.
```bash
yeelight-srv -p 9800
```
*Note: You can pass `-p` or `--port` to specify a custom web service port (default is 9800).*

## API Endpoints

### 1. Acquire device inventory
- **URL:** `/api/lights`
- **Method:** `GET`
- **Description:** Returns a list of all available devices on the local network, including their IP address, hardware ID, model code, current color temperature (Kelvin), and brightness percentage.
- **Example:** `http://localhost:9800/api/lights`

### 2. Configure light state
- **URL:** `/api/set`
- **Method:** `GET`
- **Parameters:**
  - `id` (string): The specific hardware ID of the device.
  - `temp` (integer): The target color temperature in Kelvin.
  - `brightness` (float): The target brightness level from 0 to 1 (0 triggers power off, values between 0 and 1 trigger Moonlight Mode if supported, and 1 triggers 100% Normal Mode).
- **Description:** Sequentially configures the color temperature and brightness level for the specified device with a 1-second linear transition.
- **Example:** `http://localhost:9800/api/set?id=0x000000002ce4355f&temp=4800&brightness=1`
