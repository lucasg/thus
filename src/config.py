#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  config.py
#  
#  Copyright 2013 Manjaro
#  Copyright 2013 Cinnarch
#  
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#  
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#  
#  Manjaro Team:
#   Roland Singer (singro)   <roland.manjaro.org>
#   Philip Müller (philm)    <philm.manjaro.org>
#   Guillaume Benoit (guinux)<guillaume.manjaro.org>
#  
#  Cinnarch Team:
#   Alex Filgueira (faidoc) <alexfilgueira.cinnarch.com>
#   Raúl Granados (pollitux) <raulgranados.cinnarch.com>
#   Gustau Castells (karasu) <karasu.cinnarch.com>
#   Kirill Omelchenko (omelcheck) <omelchek.cinnarch.com>
#   Marc Miralles (arcnexus) <arcnexus.cinnarch.com>
#   Alex Skinner (skinner) <skinner.cinnarch.com>

import queue

class Settings():
    def __init__(self):
        # Create a queue one element size
        self.settings = queue.Queue(1)

        self.settings.put( { \
            'THUS_DIR' : '/usr/share/thus/', \
            'UI_DIR' : '/usr/share/thus/ui/', \
            'DATA_DIR' : '/usr/share/thus/data/', \
            'TMP_DIR' : '/tmp', \
            'ROOT_SQF' : 'root-image', \
            'DE_SQF' : 'xfce-image', \
            'KERNEL' : 'linux38', \
            'KERNR' : '38', \
            'language_name' : '', \
            'language_code' : '', \
            'locale' : '', \
            'keyboard_layout' : '', \
            'keyboard_variant' : '', \
            'timezone_human_zone' : '', \
            'timezone_country' : '', \
            'timezone_zone' : '', \
            'timezone_human_country' : '', \
            'timezone_comment' : '', \
            'timezone_latitude' : 0, \
            'timezone_longitude' : 0, \
            'timezone_done' : False, \
            'use_ntp' : True, \
            'partition_mode' : 'easy', \
            'auto_device' : '/dev/sda', \
            'log_file' : '/tmp/thus.log', \
            'fullname' : '', \
            'hostname' : 'manjaro', \
            'username' : '', \
            'password' : '', \
            'require_password' : True, \
            'encrypt_home' : False, \
            'user_info_done' : False, \
            'rankmirrors_done' : False })

    def _get_settings(self):
        gd = self.settings.get()
        d = gd.copy()
        self.settings.put(gd)
        return d

    def _update_settings(self, d):
        gd = self.settings.get()
        try:
            gd.update(d)
        finally:
            self.settings.put(gd)
        
    def get(self, key):
        d = self._get_settings()
        return d[key]
        
    def set(self, key, value):
        d = self._get_settings()
        d[key] = value
        self._update_settings(d)
