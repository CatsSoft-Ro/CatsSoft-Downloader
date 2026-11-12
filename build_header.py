
#-------------------------------------------------------------------------------
# CatsSoft Downloader v6.13 - Adaptive Plexo-Inspired Engine
# Features: Thread-Safe, Proper Error Detection, Segment Validation, No Memory Leaks
# Changelog v6.13: reluare pe retry, chunk adaptiv în segmente, conexiune SQLite
# reutilizată, salvare atomică a setărilor, ETA pe card, curățare fișiere orfane.
# Author: Andruska
# Licence: MIT
#-------------------------------------------------------------------------------

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import requests
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from urllib.parse import urlparse, parse_qs, unquote
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List
from datetime import datetime, timedelta
from PIL import Image, ImageDraw
import sqlite3
import json
from pathlib import Path
import logging
import weakref
import re
import math

# Configure logging
# Consolele Windows folosesc frecvent cp1250/cp1252, care nu conțin litera „ț"
# (U+021B). Fără reconfigurare, fiecare mesaj în română se pierde cu
# „--- Logging error ---" în loc să fie afișat.
