import asyncio
import logging
import signal
import sys
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

from config import CHAT_ID, load_accounts, now_london, validate_env
