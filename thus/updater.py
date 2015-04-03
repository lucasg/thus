#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  updater.py
#
#  This file was forked from Cnchi (graphical installer from Antergos)
#  Check it at https://github.com/antergos
#
#  Copyright 2013 Antergos (http://antergos.com/)
#  Copyright 2013 Manjaro (http://manjaro.org)
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

""" Module to update Thus """

import json
import hashlib
import os
import logging
import shutil

import misc.misc as misc
import download.download_urllib as download
import info

_branch="devel"
_update_info_url = "https://raw.github.com/manjaro/thus/{0}/update.info".format(_branch)
_zip_url = "https://github.com/manjaro/thus/archive/{0}.zip".format(_branch)
_update_info = "/usr/share/thus/update.info"

_src_dir = os.path.dirname(__file__) or '.'
_base_dir = os.path.join(_src_dir, "..")


def get_md5_from_file(filename):
    with open(filename, 'rb') as myfile:
        buf = myfile.read()
        md5 = get_md5_from_text(buf)
    return md5


def get_md5_from_text(text):
    """ Gets md5 hash from str """
    md5 = hashlib.md5()
    md5.update(text)
    return md5.hexdigest()


class Updater():
    def __init__(self, force_update):
        self.remote_version = ""

        self.md5s = {}

        self.force = force_update

        if not os.path.exists(_update_info):
            logging.warning(_("Could not find 'update.info' file. Thus will not be able to update itself."))
            return

        # Get local info (local update.info)
        with open(_update_info, "r") as local_update_info:
            response = local_update_info.read()
            if len(response) > 0:
                update_info = json.loads(response)
                self.local_files = update_info['files']

        # Download update.info (contains info of all Thus's files)
        request = download.url_open(_devel_update_info_url)

        if request is not None:
            response = request.read().decode('utf-8')
            if len(response) > 0:
                update_info = json.loads(response)
                self.remote_version = update_info['version']
                for remote_file in update_info['files']:
                    self.md5s[remote_file['name']] = remote_file['md5']
                logging.info(_("Thus Internet version: %s"), self.remote_version)
                self.force = force_update

    def is_remote_version_newer(self):
        """ Returns true if the Internet version of Thus is newer than the local one """

        if len(self.remote_version) < 1:
            return False

        # Version is always: x.y.z
        local_ver = info.THUS_VERSION.split(".")
        remote_ver = self.remote_version.split(".")

        local = [int(local_ver[0]), int(local_ver[1]), int(local_ver[2])]
        remote = [int(remote_ver[0]), int(remote_ver[1]), int(remote_ver[2])]

        if remote[0] > local[0]:
            return True

        if remote[0] == local[0] and remote[1] > local[1]:
            return True

        if remote[0] == local[0] and remote[1] == local[1] and remote[2] > local[2]:
            return True

        return False

    def should_update_local_file(self, remote_name, remote_md5):
        """ Checks if remote file is different from the local one (just compares md5)"""
        for local_file in self.local_files:
            if local_file['name'] == remote_name and local_file['md5'] != remote_md5 and '__' not in local_file['name']:
                return True
        return False

    def update(self):
        """ Check if a new version is available and
            update all files only if necessary (or forced) """
        update_thus = False

        if self.is_remote_version_newer():
            logging.info(_("New version found. Updating installer..."))
            update_thus = True
        elif self.force:
            logging.info(_("No new version found. Updating anyways..."))
            update_thus = True

        if update_thus:
            logging.debug(_("Downloading new version of Thus..."))
            zip_path = "/tmp/thus-{0}.zip".format(self.remote_version)
            res = self.download_master_zip(zip_path)
            if not res:
                logging.error(_("Can't download new Thus version."))
                return False

            # master.zip file is downloaded, we must unzip it
            logging.debug(_("Uncompressing new version..."))
            try:
                self.unzip_and_copy(zip_path)
            except Exception as err:
                logging.error(err)
                return False

        return update_thus

    @staticmethod
    def download_master_zip(zip_path):
        """ Download new Thus version from github """
        request = download.url_open(_devel_zip_url)

        if request is None:
            return False

        if not os.path.exists(zip_path):
            with open(zip_path, 'wb') as zip_file:
                (data, error) = download.url_open_read(request)

                while len(data) > 0 and not error:
                    zip_file.write(data)
                    (data, error) = download.url_open_read(request)

                if error:
                    return False
        return True

    def unzip_and_copy(self, zip_path):
        """ Unzip (decompress) a zip file using zipfile standard module """
        import zipfile

        dst_dir = "/tmp"

        with zipfile.ZipFile(zip_path) as zip_file:
            for member in zip_file.infolist():
                zip_file.extract(member, dst_dir)
                full_path = os.path.join(dst_dir, member.filename)
                dst_full_path = os.path.join("/usr/share/thus", full_path.split("/tmp/thus-{0}/".format(_branch))[1])
                if os.path.isfile(dst_full_path) and dst_full_path in self.md5s:
                    if self.md5s[dst_full_path] == get_md5_from_file(full_path):
                        try:
                            with misc.raised_privileges():
                                shutil.copyfile(full_path, dst_full_path)
                        except FileNotFoundError as file_error:
                            logging.error(_("Can't copy %s to %s"), full_path, dst_full_path)
                            logging.error(file_error)
                    else:
                        logging.warning(_("Wrong md5. Bad download or wrong file, won't update this one"))
