#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  installation_process.py
#
#  This file was forked from Cnchi (graphical installer from Antergos)
#  Check it at https://github.com/antergos
#
#  Copyright © 2013-2015 Antergos (http://antergos.com/)
#  Copyright © 2013-2015 Manjaro (http://manjaro.org)
#
#  This file is part of Thus.
#
#  Thus is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  Thus is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Thus; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

""" Installation thread module. Where the real installation happens """

import crypt
import logging
import multiprocessing
import os
import collections
import platform
import queue
import shutil
import subprocess
import sys
import time
 
import traceback

import parted3.fs_module as fs
import misc.misc as misc
import encfs
from installation import auto_partition
from installation import chroot
from installation import mkinitcpio

from configobj import ConfigObj

conf_file = '/etc/thus.conf'
configuration = ConfigObj(conf_file)
MHWD_SCRIPT = 'mhwd.sh'
DEST_DIR = "/install"

DesktopEnvironment = collections.namedtuple('DesktopEnvironment', ['executable', 'desktop_file'])

desktop_environments = [
    DesktopEnvironment('/usr/bin/startkde', 'plasma'),  # KDE Plasma 5
    DesktopEnvironment('/usr/bin/startkde', 'kde-plasma'),  # KDE Plasma 4
    DesktopEnvironment('/usr/bin/gnome-session', 'gnome'),
    DesktopEnvironment('/usr/bin/startxfce4', 'xfce'),
    DesktopEnvironment('/usr/bin/cinnamon-session', 'cinnamon-session'),
    DesktopEnvironment('/usr/bin/mate-session', 'mate'),
    DesktopEnvironment('/usr/bin/enlightenment_start', 'enlightenment'),
    DesktopEnvironment('/usr/bin/lxsession', 'LXDE'),
    DesktopEnvironment('/usr/bin/startlxde', 'LXDE'),
    DesktopEnvironment('/usr/bin/lxqt-session', 'lxqt'),
    DesktopEnvironment('/usr/bin/pekwm', 'pekwm'),
    DesktopEnvironment('/usr/bin/openbox-session', 'openbox')
]


def chroot_run(cmd):
    chroot.run(cmd, DEST_DIR)


def write_file(filecontents, filename):
    """ writes a string of data to disk """
    if not os.path.exists(os.path.dirname(filename)):
        os.makedirs(os.path.dirname(filename))

    with open(filename, "w") as fh:
        fh.write(filecontents)

# BEGIN: RSYNC-based file copy support
# CMD = 'unsquashfs -f -i -da 32 -fr 32 -d %(dest)s %(source)s'
CMD = 'rsync -ar --progress %(source)s %(dest)s'
PERCENTAGE_FORMAT = '%d/%d ( %.2f %% )'
from threading import Thread
import re
ON_POSIX = 'posix' in sys.builtin_module_names


class FileCopyThread(Thread):
    """ Update the value of the progress bar so that we get some movement """
    def __init__(self, installer, current_file, total_files, source, dest, offset=0):
        # Environment used for executing rsync properly
        # Setting locale to C (fix issue with tr_TR locale)
        self.at_env = os.environ
        self.at_env["LC_ALL"] = "C"

        self.our_current = current_file
        self.process = subprocess.Popen(
            (CMD % {
                'source': source,
                'dest': dest,
            }).split(),
            env=self.at_env,
            bufsize=1,
            stdout=subprocess.PIPE,
            close_fds=ON_POSIX
        )
        self.installer = installer
        self.total_files = total_files
        # in order for the progressbar to pick up where the last rsync ended,
        # we need to set the offset because the total number of files is
        # calculated before.
        self.offset = offset
        super(FileCopyThread, self).__init__()

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()

    def update_label(self, text):
        self.installer.queue_event('info', _("Copying '/{0}'").format(text))

    def update_progress(self, num_files):
        progress = (float(num_files) / float(self.total_files))
        self.installer.queue_event('percent', progress)
        #self.installer.queue_event('progress-info', PERCENTAGE_FORMAT % (num_files, self.total_files, (progress*100)))

    def run(self):
        num_files_copied = 0
        for line in iter(self.process.stdout.readline, b''):
            # small comment on this regexp.
            # rsync outputs three parameters in the progress.
            # xfer#x => i try to interpret it as 'file copy try no. x'
            # to-check=x/y, where:
            #  - x = number of files yet to be checked
            #  - y = currently calculated total number of files.
            # but if you're copying directory with some links in it, the xfer#
            # might not be a reliable counter. For one increase of xfer, many
            # files may be created.
            # In case of Manjaro, we pre-compute the total number of files.
            # Therefore we can easily subtract x from y in order to get real
            # files copied / processed count.
            m = re.findall(r'xfr#(\d+), ir-chk=(\d+)/(\d+)', line.decode())
            if m:
                # we've got a percentage update
                num_files_remaining = int(m[0][1])
                num_files_total_local = int(m[0][2])
                # adjusting the offset so that progressbar can be continuesly drawn
                num_files_copied = num_files_total_local - num_files_remaining + self.offset
                if num_files_copied % 100 == 0:
                    self.update_progress(num_files_copied)
            # Disabled until we find a proper solution for BadDrawable
            # (invalid Pixmap or Window parameter) errors
            # Details: serial YYYYY error_code 9 request_code 62 minor_code 0
            # This might even speed up the copy process ...
            """else:
                # we've got a filename!
                if num_files_copied % 100 == 0:
                    self.update_label(line.decode().strip())"""

        self.offset = num_files_copied

# END: RSYNC-based file copy support


class InstallError(Exception):
    """ Exception class called upon an installer error """
    def __init__(self, value):
        """ Initialize exception class """
        super().__init__(value)
        self.value = value

    def __str__(self):
        """ Returns exception message """
        return repr(self.value)


class InstallationProcess(multiprocessing.Process):
    """ Installation process thread class """
    def __init__(self, settings, callback_queue, mount_devices,
                 fs_devices, ssd=None, blvm=False):
        """ Initialize installation class """
        multiprocessing.Process.__init__(self)

        self.callback_queue = callback_queue
        self.settings = settings
        self.method = self.settings.get('partition_mode')
        msg = _("Installing using the '{0}' method").format(self.method)
        self.queue_event('info', msg)

        # This flag tells us if there is a lvm partition (from advanced install)
        # If it's true we'll have to add the 'lvm2' hook to mkinitcpio
        self.blvm = blvm

        if ssd is not None:
            self.ssd = ssd
        else:
            self.ssd = {}

        self.mount_devices = mount_devices

        # Set defaults
        self.desktop_manager = 'none'
        self.network_manager = 'NetworkManager'
        self.card = []
        # Packages to be removed
        self.conflicts = []

        self.fs_devices = fs_devices

        self.running = True
        self.error = False

        self.special_dirs_mounted = False

        self.auto_device = self.settings.get('auto_device')
        self.arch = platform.machine()
        self.bootloader_ok = self.settings.get('bootloader_ok')

        # get thus.conf settings
        self.distribution_name = configuration['distribution']['DISTRIBUTION_NAME']
        self.distribution_version = configuration['distribution']['DISTRIBUTION_VERSION']
        self.live_user = configuration['install']['LIVE_USER_NAME']
        self.media = configuration['install']['LIVE_MEDIA_SOURCE']
        self.media_desktop = configuration['install']['LIVE_MEDIA_DESKTOP']
        self.media_type = configuration['install']['LIVE_MEDIA_TYPE']

    def queue_fatal_event(self, txt):
        """ Queues the fatal event and exits process """
        self.error = True
        self.running = False
        self.queue_event('error', txt)
        self.callback_queue.join()
        # Is this really necessary?
        os._exit(0)

    def queue_event(self, event_type, event_text=""):
        if self.callback_queue is not None:
            try:
                self.callback_queue.put_nowait((event_type, event_text))
            except queue.Full:
                pass
        else:
            print("{0}: {1}".format(event_type, event_text))

    def wait_for_empty_queue(self, timeout):
        if self.callback_queue is not None:
            tries = 0
            if timeout < 1:
                timeout = 1
            while tries < timeout and not self.callback_queue.empty():
                time.sleep(1)
                tries += 1

    def run(self):
        """ Calls run_installation and takes care of exceptions """

        try:
            self.run_installation()
        except subprocess.CalledProcessError as process_error:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            trace = repr(traceback.format_exception(exc_type, exc_value, exc_traceback))
            logging.error(_("Error running command {0}".format(process_error.cmd)))
            logging.error(_("Output: {0}".format(process_error.output)))
            logging.error(trace)
            self.queue_fatal_event(process_error.output)
        except (
                InstallError, KeyboardInterrupt, TypeError, AttributeError, OSError,
                IOError) as install_error:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            trace = repr(traceback.format_exception(exc_type, exc_value, exc_traceback))
            logging.error(install_error)
            logging.error(trace)
            self.queue_fatal_event(install_error)

    @misc.raise_privileges
    def run_installation(self):
        """
        Run installation

        From this point, on a warning situation, Thus should try to continue,
        so we need to catch the exception here.
        If we don't catch the exception here, it will be catched in run() and
        managed as a fatal error.
        On the other hand, if we want to clarify the exception message we can
        catch it here and then raise an InstallError exception.
        """

        # Create the directory where we will mount our new root partition
        try:
            os.makedirs(DEST_DIR)
        except OSError:
            # If we're recovering from a failed/stoped install, there'll be
            # some mounted directories. Try to unmount them first.
            auto_partition.unmount_all(DEST_DIR)

        # Create, format and mount partitions in automatic mode
        if self.method == 'automatic':
            logging.debug(_("Creating partitions and their filesystems in {0}".format(self.auto_device)))

            # If no key password is given a key file is generated and stored in /boot
            # (see auto_partition.py)

            auto = auto_partition.AutoPartition(
                dest_dir=DEST_DIR,
                auto_device=self.auto_device,
                use_luks=self.settings.get("use_luks"),
                luks_password=self.settings.get("luks_root_password"),
                use_lvm=self.settings.get("use_lvm"),
                use_home=self.settings.get("use_home"),
                bootloader=self.settings.get("bootloader"),
                callback_queue=self.callback_queue
            )
            auto.run()

            # used in modify_grub_default()
            self.mount_devices = auto.get_mount_devices()
            # used when configuring fstab
            self.fs_devices = auto.get_fs_devices()

        # In advanced mode we only need to mount partitions
        if self.method == 'advanced':
            for path in sorted(self.mount_devices):
                if path == "" or path == "swap":
                    continue
                mount_part = self.mount_devices[path]
                mount_dir = DEST_DIR + path
                if not os.path.exists(mount_dir):
                    os.makedirs(mount_dir)
                try:
                    if not os.path.exists(mount_dir):
                        os.makedirs(mount_dir)
                    txt = _("Mounting partition {0} into {1} directory")
                    txt = txt.format(mount_part, mount_dir)
                    logging.debug(txt)
                    subprocess.check_call(['mount', mount_part, mount_dir])
                except subprocess.CalledProcessError as err:
                    logging.warning(_("Can't mount {0} in {1}"
                                      .format(mount_part, mount_dir)))
                    logging.warning(_("Command {0} has failed."
                                      .format(err.cmd)))
                    logging.warning(_("Output : {0}".format(err.output)))

        # Nasty workaround:
        # If pacman was stoped and /var is in another partition than root
        # (so as to be able to resume install), database lock file will still
        # be in place.
        # We must delete it or this new installation will fail
        db_lock = os.path.join(DEST_DIR, "var/lib/pacman/db.lck")
        if os.path.exists(db_lock):
            with misc.raised_privileges():
                os.remove(db_lock)
            logging.debug(_("{0} deleted".format(db_lock)))

        # Create some needed folders
        os.makedirs(os.path.join(DEST_DIR, 'var/lib/pacman'), exist_ok=True)
        os.makedirs(os.path.join(DEST_DIR, 'etc/pacman.d/gnupg'), exist_ok=True)
        os.makedirs(os.path.join(DEST_DIR, 'var/log'), exist_ok=True)

        all_ok = True

        try:
            self.queue_event('debug', _('Install System ...'))
            self.install_system()
            self.queue_event('debug', _('System installed.'))
            self.queue_event('debug', _('Configuring system ...'))
            self.configure_system()
            self.queue_event('debug', _('System configured.'))

        except subprocess.CalledProcessError as err:
            logging.error(err)
            self.queue_fatal_event("CalledProcessError.output = {0}".format(err.output))
            all_ok = False
        except InstallError as err:
            logging.error(err)
            self.queue_fatal_event(err.value)
            all_ok = False
        except Exception as err:
            try:
                logging.debug('Exception: {0}. Trying to continue.'.format(err))
                all_ok = True
                pass
            except Exception as err:
                txt = ('Unknown Error: {0}. Unable to continue.'.format(err))
                logging.debug(txt)
                self.queue_fatal_event(txt)
                self.running = False
                self.error = True
                all_ok = False

        if all_ok is False:
            self.error = True
            return False
        else:
            # Last but not least, copy Thus log to new installation
            datetime = time.strftime("%Y%m%d") + "-" + time.strftime("%H%M%S")
            dst = os.path.join(DEST_DIR,
                               "var/log/thus-{0}.log".format(datetime))
            try:
                shutil.copy("/tmp/thus.log", dst)
            except FileNotFoundError:
                logging.warning(_("Can't copy Thus log to {0}".format(dst)))
            except FileExistsError:
                pass
            
            source_dirs = ["/source", "/source_desktop"]

            partition_dirs = []
            for path in sorted(self.mount_devices, reverse=True):
                if path == "" or path == "swap" or path == "/":
                    continue
                partition_dirs += [DEST_DIR + path]

            install_dirs = ["/install"]
            unmount_points = source_dirs + partition_dirs + install_dirs

            logging.debug("Paths to unmount: {0}".format(unmount_points))
            for p in unmount_points:
                (fsname, fstype, writable) = misc.mount_info(p)
                if fsname:
                    logging.debug(_("Unmounting {0}".format(p)))
                    try:
                        subprocess.check_call(['umount', p])
                    except subprocess.CalledProcessError:
                        logging.debug("Can't unmount. Try -l to force it.")
                        try:
                            subprocess.check_call(["umount", "-l", p])
                        except subprocess.CalledProcessError as err:
                            logging.warning(_("Unable to umount {0}".format(p)))
                            logging.warning(_("Command {0} has failed."
                                              .format(err.cmd)))
                            logging.warning(_("Output : {0}"
                                              .format(err.output)))

            # Installation finished successfully
            self.queue_event("finished", _("Installation finished successfully."))
            self.running = False
            self.error = False
            return True

    @staticmethod
    def check_source_folder(mount_point):
        """ Check if source folders are mounted """
        device = None
        with open('/proc/mounts', 'r') as fp:
            for line in fp:
                line = line.split()
                if line[1] == mount_point:
                    device = line[0]
        return device

    def install_system(self):
        """ Copies all files to target """
        # mount the media location.
        try:
            os.makedirs(DEST_DIR, exist_ok=True)
            os.makedirs("/source", exist_ok=True)
            os.makedirs("/source_desktop", exist_ok=True)

            # find the squashfs..
            if not os.path.exists(self.media):
                txt = _("Base filesystem does not exist! Critical error (exiting).")
                logging.error(txt)
                self.queue_fatal_event(txt)
            if not os.path.exists(self.media_desktop):
                txt = _("Desktop filesystem does not exist! Critical error (exiting).")
                logging.error(txt)
                self.queue_fatal_event(txt)

            # Mount the installation media
            mount_point = "/source"
            device = self.check_source_folder(mount_point)
            if device is None:
                subprocess.check_call(["mount",
                                       self.media,
                                       mount_point,
                                       "-t",
                                       self.media_type,
                                       "-o",
                                       "loop"])
            else:
                logging.warning(_("{0} is already mounted at {1} as {2}"
                                  .format(self.media, mount_point, device)))

            mount_point = "/source_desktop"
            device = self.check_source_folder(mount_point)
            if device is None:
                subprocess.check_call(["mount",
                                       self.media_desktop,
                                       mount_point,
                                       "-t",
                                       self.media_type,
                                       "-o",
                                       "loop"])
            else:
                logging.warning(_("{0} is already mounted at {1} as {2}"
                                  .format(self.media_desktop, mount_point, device)))

            # walk root filesystem
            SOURCE = "/source/"
            directory_times = []
            # index the files
            self.queue_event('info', _("Indexing files of root-image to be copied ..."))
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc", "-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output1 = p2.communicate()[0]
            self.queue_event('info', _("Indexing files of desktop-image to be copied ..."))
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media_desktop], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc", "-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output2 = p2.communicate()[0]
            our_total = int(float(output1) + float(output2))
            self.queue_event('info', _("Extracting root-image ..."))
            our_current = 0
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST_DIR)
            t.start()
            t.join()

            # walk desktop filesystem
            SOURCE = "/source_desktop/"
            DEST = DEST_DIR
            directory_times = []
            self.queue_event('info', _("Extracting desktop-image ..."))
            our_current = int(output1)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST_DIR, t.offset)
            t.start()
            t.join()

            # this is purely out of aesthetic reasons. Because we're reading of
            # the queue once 3 seconds, good chances are we're going to miss
            # the 100% file copy. Yherefore it would be nice to show 100% to
            # the user so he doesn't panick that not all of the files copied.
            self.queue_event('percent', 1.00)
            self.queue_event('progress-info', PERCENTAGE_FORMAT % (our_total, our_total, 100))
            for dirtime in directory_times:
                (directory, atime, mtime) = dirtime
                try:
                    self.queue_event('info', _("Restoring meta-information on {0}".format(directory)))
                    os.utime(directory, (atime, mtime))
                except OSError:
                    pass

        except Exception as err:
            logging.error(err)
            self.queue_fatal_event(err)
            import traceback
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)

    def is_running(self):
        """ Checks if thread is running """
        return self.running

    def is_ok(self):
        """ Checks if an error has been issued """
        return not self.error

    @staticmethod
    def copy_network_config():
        """ Copies Network Manager configuration """
        source_nm = "/etc/NetworkManager/system-connections/"
        target_nm = os.path.join(DEST_DIR,
                                 "etc/NetworkManager/system-connections")

        # Sanity checks.  We don't want to do anything if a network
        # configuration already exists on the target
        if os.path.exists(source_nm) and os.path.exists(target_nm):
            for network in os.listdir(source_nm):
                # Skip LTSP live
                if network == "LTSP":
                    continue

                source_network = os.path.join(source_nm, network)
                target_network = os.path.join(target_nm, network)

                if os.path.exists(target_network):
                    continue

                try:
                    shutil.copy(source_network, target_network)
                except FileNotFoundError:
                    logging.warning(_("Can't copy network configuration files"))
                except FileExistsError:
                    pass

    def auto_fstab(self):
        """ Create /etc/fstab file """

        all_lines = [
            "# /etc/fstab: static file system information.",
            "#",
            "# Use 'blkid' to print the universally unique identifier for a",
            "# device; this may be used with UUID= as a more robust way to name devices",
            "# that works even if disks are added and removed. See fstab(5).",
            "#",
            "# <file system> <mount point>   <type>  <options>       <dump>  <pass>",
            "#"]

        use_luks = self.settings.get("use_luks")
        use_lvm = self.settings.get("use_lvm")

        for mount_point in self.mount_devices:
            partition_path = self.mount_devices[mount_point]
            part_info = fs.get_info(partition_path)
            uuid = part_info['UUID']

            if partition_path in self.fs_devices:
                myfmt = self.fs_devices[partition_path]
            else:
                # It hasn't any filesystem defined, skip it.
                continue

            # Take care of swap partitions
            if "swap" in myfmt:
                # If using a TRIM supported SSD, discard is a valid mount option for swap
                if partition_path in self.ssd:
                    opts = "defaults,discard"
                else:
                    opts = "defaults"
                txt = "UUID={0} swap swap {1} 0 0".format(uuid, opts)
                all_lines.append(txt)
                logging.debug(_("Added to fstab : {0}".format(txt)))
                continue

            crypttab_path = os.path.join(DEST_DIR, 'etc/crypttab')

            # Fix for home + luks, no lvm (from Automatic Install)
            if ("/home" in mount_point and self.method == "automatic" and use_luks and not use_lvm):
                # Modify the crypttab file
                luks_root_password = self.settings.get("luks_root_password")
                if luks_root_password and len(luks_root_password) > 0:
                    # Use password and not a keyfile
                    home_keyfile = "none"
                else:
                    # Use a keyfile
                    home_keyfile = "/etc/luks-keys/home"

                os.chmod(crypttab_path, 0o666)
                with open(crypttab_path, 'a') as crypttab_file:
                    line = "cryptManjaroHome /dev/disk/by-uuid/{0} {1} luks\n".format(uuid, home_keyfile)
                    crypttab_file.write(line)
                    logging.debug(_("Added to crypttab : {0}"), line)
                os.chmod(crypttab_path, 0o600)

                # Add line to fstab
                txt = "/dev/mapper/cryptManjaroHome {0} {1} defaults 0 0".format(mount_point, myfmt)
                all_lines.append(txt)
                logging.debug(_("Added to fstab : {0}".format(txt)))
                continue

            # Add all LUKS partitions from Advanced Install (except root).
            if self.method == "advanced" and mount_point is not "/" and use_luks and "/dev/mapper" in partition_path:
                os.chmod(crypttab_path, 0o666)
                vol_name = partition_path[len("/dev/mapper/"):]
                with open(crypttab_path, 'a') as crypttab_file:
                    line = "{0} /dev/disk/by-uuid/{1} none luks\n".format(vol_name, uuid)
                    crypttab_file.write(line)
                    logging.debug(_("Added to crypttab : {0}".format(line)))
                os.chmod(crypttab_path, 0o600)

                txt = "{0} {1} {2} defaults 0 0".format(partition_path, mount_point, myfmt)
                all_lines.append(txt)
                logging.debug(_("Added to fstab : {0}".format(txt)))
                continue

            # fstab uses vfat to mount fat16 and fat32 partitions
            if "fat" in myfmt:
                myfmt = 'vfat'

            if "btrfs" in myfmt:
                self.settings.set('btrfs', True)

            # Avoid adding a partition to fstab when it has no mount point (swap has been checked above)
            if mount_point == "":
                continue

            # Create mount point on destination system if it yet doesn't exist
            full_path = DEST_DIR + mount_point
            if not os.path.exists(full_path):
                os.makedirs(full_path)

            # Is ssd ?
            # Device list example: {'/dev/sdb': False, '/dev/sda': True}
            logging.debug(_("Device list : {0}".format(self.ssd)))
            device = re.sub("[0-9]+$", "", partition_path)
            is_ssd = self.ssd.get(device)
            logging.debug(_("Device: {0}, SSD: {1}".format(device, is_ssd)))

            # Add mount options parameters
            if not is_ssd:
                if "btrfs" in myfmt:
                    opts = 'defaults,rw,relatime,space_cache,autodefrag,inode_cache'
                elif "f2fs" in myfmt:
                    opts = 'defaults,rw,noatime'
                elif "ext3" in myfmt or "ext4" in myfmt:
                    opts = 'defaults,rw,relatime,data=ordered'
                else:
                    opts = "defaults,rw,relatime"
            else:
                # As of linux kernel version 3.7, the following
                # filesystems support TRIM: ext4, btrfs, JFS, and XFS.
                if myfmt == 'ext4' or myfmt == 'jfs' or myfmt == 'xfs':
                    opts = 'defaults,rw,noatime,discard'
                elif myfmt == 'btrfs':
                    opts = 'defaults,rw,noatime,compress=lzo,ssd,discard,space_cache,autodefrag,inode_cache'
                else:
                    opts = 'defaults,rw,noatime'

            no_check = ["btrfs", "f2fs"]

            if mount_point == "/" and myfmt not in no_check:
                chk = '1'
            else:
                chk = '0'

            if mount_point == "/":
                self.settings.set('ruuid', uuid)

            txt = "UUID={0} {1} {2} {3} 0 {4}".format(uuid, mount_point, myfmt, opts, chk)
            all_lines.append(txt)
            logging.debug(_("Added to fstab : {0}".format(txt)))

        # Create tmpfs line in fstab
        tmpfs = "tmpfs /tmp tmpfs defaults,noatime,mode=1777 0 0"
        all_lines.append(tmpfs)
        logging.debug(_("Added to fstab : {0}".format(tmpfs)))

        full_text = '\n'.join(all_lines) + '\n'

        fstab_path = os.path.join(DEST_DIR, 'etc/fstab')
        with open(fstab_path, 'w') as fstab_file:
            fstab_file.write(full_text)

        logging.debug(_("fstab written."))

    @staticmethod
    def enable_services(services):
        """ Enables all services that are in the list 'services' """
        for name in services:
            path = os.path.join(DEST_DIR, "usr/lib/systemd/system/{0}.service".format(name))
            if os.path.exists(path):
                chroot_run(['systemctl', '-f', 'enable', name])
                logging.debug(_("Enabled {0} service.".format(name)))
            else:
                logging.warning(_("Can't find service {0}".format(name)))

    @staticmethod
    def change_user_password(user, new_password):
        """ Changes the user's password """
        try:
            shadow_password = crypt.crypt(new_password, "$6${0}$".format(user))
        except:
            logging.warning(_("Error creating password hash for user {0}".format(user)))
            return False

        try:
            chroot_run(['usermod', '-p', shadow_password, user])
        except:
            logging.warning(_("Error changing password for user {0}".format(user)))
            return False

        return True

    @staticmethod
    def auto_timesetting():
        """ Set hardware clock """
        subprocess.check_call(["hwclock", "--systohc", "--utc"])
        shutil.copy2("/etc/adjtime", os.path.join(DEST_DIR, "etc/"))

    @staticmethod
    def uncomment_locale_gen(locale):
        """ Uncomment selected locale in /etc/locale.gen """

        path = os.path.join(DEST_DIR, "etc/locale.gen")

        if os.path.exists(path):
            with open(path) as gen:
                text = gen.readlines()

            with open(path, "w") as gen:
                for line in text:
                    if locale in line and line[0] == "#":
                        # remove trailing '#'
                        line = line[1:]
                    gen.write(line)
        else:
            logging.warning(_("Can't find locale.gen file"))

    @staticmethod
    def check_output(command):
        """ Helper function to run a command """
        return subprocess.check_output(command.split()).decode().strip("\n")

    def find_desktop_environment(self):
        for desktop_environment in desktop_environments:
            if os.path.exists('{0}{1}'.format(DEST_DIR, desktop_environment.executable)) \
               and os.path.exists('{0}/usr/share/xsessions/{1}.desktop'.format(DEST_DIR, desktop_environment.desktop_file)):
                return desktop_environment
        return None

    @staticmethod
    def alsa_mixer_setup():
        """ Sets ALSA mixer settings """

        cmds = [
            "Master 70% unmute",
            "Front 70% unmute"
            "Side 70% unmute"
            "Surround 70% unmute",
            "Center 70% unmute",
            "LFE 70% unmute",
            "Headphone 70% unmute",
            "Speaker 70% unmute",
            "PCM 70% unmute",
            "Line 70% unmute",
            "External 70% unmute",
            "FM 50% unmute",
            "Master Mono 70% unmute",
            "Master Digital 70% unmute",
            "Analog Mix 70% unmute",
            "Aux 70% unmute",
            "Aux2 70% unmute",
            "PCM Center 70% unmute",
            "PCM Front 70% unmute",
            "PCM LFE 70% unmute",
            "PCM Side 70% unmute",
            "PCM Surround 70% unmute",
            "Playback 70% unmute",
            "PCM,1 70% unmute",
            "DAC 70% unmute",
            "DAC,0 70% unmute",
            "DAC,1 70% unmute",
            "Synth 70% unmute",
            "CD 70% unmute",
            "Wave 70% unmute",
            "Music 70% unmute",
            "AC97 70% unmute",
            "Analog Front 70% unmute",
            "VIA DXS,0 70% unmute",
            "VIA DXS,1 70% unmute",
            "VIA DXS,2 70% unmute",
            "VIA DXS,3 70% unmute",
            "Mic 70% mute",
            "IEC958 70% mute",
            "Master Playback Switch on",
            "Master Surround on",
            "SB Live Analog/Digital Output Jack off",
            "Audigy Analog/Digital Output Jack off"]

        for cmd in cmds:
            chroot_run(['sh', '-c', 'amixer -c 0 sset {0}'.format(cmd)])

        # Save settings
        chroot_run(['alsactl', '-f', '/etc/asound.state', 'store'])

    def set_autologin(self):
        """ Enables automatic login for the installed desktop manager """
        username = self.settings.get('username')
        self.queue_event('info', _("{0}: Enable automatic login for user {1}.".format(self.desktop_manager, username)))

        if self.desktop_manager == 'mdm':
            # Systems with MDM as Desktop Manager
            mdm_conf_path = os.path.join(DEST_DIR, "etc/mdm/custom.conf")
            if os.path.exists(mdm_conf_path):
                with open(mdm_conf_path, "r") as mdm_conf:
                    text = mdm_conf.readlines()
                with open(mdm_conf_path, "w") as mdm_conf:
                    for line in text:
                        if '[daemon]' in line:
                            line = '[daemon]\nAutomaticLogin={0}\nAutomaticLoginEnable=True\n'.format(username)
                        mdm_conf.write(line)
            else:
                with open(mdm_conf_path, "w") as mdm_conf:
                    mdm_conf.write('# Thus - Enable automatic login for user\n')
                    mdm_conf.write('[daemon]\n')
                    mdm_conf.write('AutomaticLogin={0}\n'.format(username))
                    mdm_conf.write('AutomaticLoginEnable=True\n')
        elif self.desktop_manager == 'gdm':
            # Systems with GDM as Desktop Manager
            gdm_conf_path = os.path.join(DEST_DIR, "etc/gdm/custom.conf")
            if os.path.exists(gdm_conf_path):
                with open(gdm_conf_path, "r") as gdm_conf:
                    text = gdm_conf.readlines()
                with open(gdm_conf_path, "w") as gdm_conf:
                    for line in text:
                        if '[daemon]' in line:
                            line = '[daemon]\nAutomaticLogin={0}\nAutomaticLoginEnable=True\n'.format(username)
                        gdm_conf.write(line)
            else:
                with open(gdm_conf_path, "w") as gdm_conf:
                    gdm_conf.write('# Thus - Enable automatic login for user\n')
                    gdm_conf.write('[daemon]\n')
                    gdm_conf.write('AutomaticLogin={0}\n'.format(username))
                    gdm_conf.write('AutomaticLoginEnable=True\n')
        elif self.desktop_manager == 'kdm':
            # Systems with KDM as Desktop Manager
            kdm_conf_path = os.path.join(DEST_DIR, "usr/share/config/kdm/kdmrc")
            text = []
            with open(kdm_conf_path, "r") as kdm_conf:
                text = kdm_conf.readlines()
            with open(kdm_conf_path, "w") as kdm_conf:
                for line in text:
                    if '#AutoLoginEnable=true' in line:
                        line = 'AutoLoginEnable=true\n'
                    if 'AutoLoginUser=' in line:
                        line = 'AutoLoginUser={0}\n'.format(username)
                    kdm_conf.write(line)
        elif self.desktop_manager == 'lxdm':
            # Systems with LXDM as Desktop Manager
            lxdm_conf_path = os.path.join(DEST_DIR, "etc/lxdm/lxdm.conf")
            text = []
            with open(lxdm_conf_path, "r") as lxdm_conf:
                text = lxdm_conf.readlines()
            with open(lxdm_conf_path, "w") as lxdm_conf:
                for line in text:
                    if '# autologin=dgod' in line:
                        line = 'autologin={0}\n'.format(username)
                    lxdm_conf.write(line)
        elif self.desktop_manager == 'lightdm':
            # Systems with LightDM as Desktop Manager
            # Ideally, we should use configparser for the ini conf file,
            # but we just do a simple text replacement for now, as it worksforme(tm)
            lightdm_conf_path = os.path.join(DEST_DIR, "etc/lightdm/lightdm.conf")
            text = []
            with open(lightdm_conf_path, "r") as lightdm_conf:
                text = lightdm_conf.readlines()
            with open(lightdm_conf_path, "w") as lightdm_conf:
                for line in text:
                    if '#autologin-user=' in line:
                        line = 'autologin-user={0}\n'.format(username)
                    lightdm_conf.write(line)
        elif self.desktop_manager == 'slim':
            # Systems with Slim as Desktop Manager
            slim_conf_path = os.path.join(DEST_DIR, "etc/slim.conf")
            text = []
            with open(slim_conf_path, "r") as slim_conf:
                text = slim_conf.readlines()
            with open(slim_conf_path, "w") as slim_conf:
                for line in text:
                    if 'auto_login' in line:
                        line = 'auto_login yes\n'
                    if 'default_user' in line:
                        line = 'default_user {0}\n'.format(username)
                    slim_conf.write(line)
        elif self.desktop_manager == 'sddm':
            # Systems with Sddm as Desktop Manager
            sddm_conf_path = os.path.join(DEST_DIR, "etc/sddm.conf")
            if os.path.isfile(sddm_conf_path):
                self.queue_event('info', "SDDM config file exists")
            else:
                chroot_run(["sh", "-c", "sddm --example-config > /etc/sddm.conf"])           
            text = []
            with open(sddm_conf_path, "r") as sddm_conf:
                text = sddm_conf.readlines()
            with open(sddm_conf_path, "w") as sddm_conf:
                for line in text:
                    # User= line, possibly commented out
                    if re.match('\\s*(?:#\\s*)?User=', line):
                        line = 'User={}\n'.format(username)
                    # Session= line, commented out or with empty value
                    if re.match('\\s*#\\s*Session=|\\s*Session=$', line):
                        default_desktop_environment = self.find_desktop_environment()
                        if default_desktop_environment != None:
                            line = 'Session={}.desktop\n'.format(default_desktop_environment.desktop_file)
                    sddm_conf.write(line)

    def configure_system(self):
        """ Final install steps
            Set clock, language, timezone
            Run mkinitcpio
            Populate pacman keyring
            Setup systemd services
            ... and more """

        # First and last thing we do here mounting/unmouting special dirs.
        chroot.mount_special_dirs(DEST_DIR)
        
        self.queue_event('pulse', 'start')
        self.queue_event('action', _("Configuring your new system"))

        self.auto_fstab()
        self.queue_event('debug', _('fstab file generated.'))

        # Copy configured networks in Live medium to target system
        if self.network_manager == 'NetworkManager':
            self.copy_network_config()

        self.queue_event('debug', _('Network configuration copied.'))

        # enable services
        # self.enable_services([self.network_manager])

        # cups_service = os.path.join(DEST_DIR, "usr/lib/systemd/system/org.cups.cupsd.service")
        # if os.path.exists(cups_service):
        #    self.enable_services(['org.cups.cupsd'])"""

        # enable targets
        # self.enable_targets(['remote-fs.target'])

        # self.queue_event('debug', 'Enabled installed services.')

        # Wait FOREVER until the user sets the timezone
        while self.settings.get('timezone_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        if self.settings.get("use_ntp"):
            self.enable_services(["ntpd"])

        # Set timezone
        zoneinfo_path = os.path.join("/usr/share/zoneinfo", self.settings.get("timezone_zone"))
        chroot_run(['ln', '-s', zoneinfo_path, "/etc/localtime"])

        self.queue_event('debug', _('Time zone set.'))

        # Wait FOREVER until the user sets his params
        while self.settings.get('user_info_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        # Set user parameters
        username = self.settings.get('username')
        fullname = self.settings.get('fullname')
        password = self.settings.get('password')
        root_password = self.settings.get('root_password')
        hostname = self.settings.get('hostname')

        sudoers_path = os.path.join(DEST_DIR, "etc/sudoers.d/10-installer")

        with open(sudoers_path, "w") as sudoers:
            sudoers.write('{0} ALL=(ALL) ALL\n'.format(username))

        subprocess.check_call(["chmod", "440", sudoers_path])

        self.queue_event('debug', _('Sudo configuration for user {0} done.'.format(username)))

        default_groups = 'lp,video,network,storage,wheel,audio'

        if self.settings.get('require_password') is False:
            chroot_run(['groupadd', 'autologin'])
            default_groups += ',autologin'

        chroot_run(['useradd', '-m', '-s', '/bin/bash', '-g', 'users', '-G', default_groups, username])

        self.queue_event('debug', _('User {0} added.'.format(username)))

        self.change_user_password(username, password)

        chroot_run(['chfn', '-f', fullname, username])

        chroot_run(['chown', '-R', '{0}:users'.format(username), "/home/{0}".format(username)])

        hostname_path = os.path.join(DEST_DIR, "etc/hostname")
        with open(hostname_path, "w") as hostname_file:
            hostname_file.write(hostname)

        self.queue_event('debug', _('Hostname  {0} set.'.format(hostname)))

        # Set root password
        if root_password is not '':
            self.change_user_password('root', root_password)
            self.queue_event('debug', _('Set root password.'))
        else:
            self.change_user_password('root', password)
            self.queue_event('debug', _('Set the same password to root.'))

        # Generate locales
        locale = self.settings.get("locale")

        self.queue_event('info', _("Generating locales ..."))
        self.uncomment_locale_gen(locale)
        chroot_run(['locale-gen'])

        locale_conf_path = os.path.join(DEST_DIR, "etc/locale.conf")
        with open(locale_conf_path, "w") as locale_conf:
            locale_conf.write('LANG={0}\n'.format(locale))

        keyboard_layout = self.settings.get("keyboard_layout")
        keyboard_variant = self.settings.get("keyboard_variant")
        # Set /etc/vconsole.conf
        vconsole_conf_path = os.path.join(DEST_DIR, "etc/vconsole.conf")
        with open(vconsole_conf_path, "w") as vconsole_conf:
            vconsole_conf.write('KEYMAP={0}\n'.format(keyboard_layout))

        # Write xorg keyboard configuration
        xorg_conf_dir = os.path.join(DEST_DIR, "etc/X11/xorg.conf.d")
        os.makedirs(xorg_conf_dir, exist_ok=True)
        fname = "{0}/etc/X11/xorg.conf.d/00-keyboard.conf".format(DEST_DIR)
        default_kb_layout = "us"
        default_kb_model = "pc105"
        with open(fname, 'w') as file:
            file.write("\n"
                       "Section \"InputClass\"\n"
                       " Identifier \"system-keyboard\"\n"
                       " MatchIsKeyboard \"on\"\n"
                       " Option \"XkbLayout\" \"{0},{1}\"\n"
                       " Option \"XkbModel\" \"{2}\"\n"
                       " Option \"XkbVariant\" \"{3},\"\n"
                       " Option \"XkbOptions\" \"{4}\"\n"
                       "EndSection\n"
                       .format(keyboard_layout, default_kb_layout,
                               default_kb_model,
                               keyboard_variant,
                               "terminate:ctrl_alt_bksp,grp:alt_shift_toggle"))

        self.queue_event('info', _("Adjusting hardware clock ..."))
        self.auto_timesetting()

        # Install configs for root
        # chroot_run(['cp', '-av', '/etc/skel/.', '/root/'])

        self.queue_event('info', _("Configuring hardware ..."))

        # Configure ALSA
        self.alsa_mixer_setup()
        logging.debug(_("Updated Alsa mixer settings"))

        '''# Set pulse
        if os.path.exists(os.path.join(DEST_DIR, "usr/bin/pulseaudio-ctl")):
            chroot_run(['pulseaudio-ctl', 'set', '75%'])'''

        # Install xf86-video driver
        if os.path.exists("/opt/livecd/pacman-gfx.conf"):
            self.queue_event('info', _("Installing drivers ..."))
            mhwd_script_path = os.path.join(self.settings.get("thus"), "scripts", MHWD_SCRIPT)
            try:
                subprocess.check_call(["/usr/bin/bash", mhwd_script_path])
                self.queue_event('debug', "Finished installing drivers.")
            except subprocess.CalledProcessError as e:
                txt = "CalledProcessError.output = {0}".format(e.output)
                logging.error(txt)
                self.queue_fatal_event(txt)
                return False

        self.queue_event('info', _("Configure display manager ..."))
        # Setup slim
        if os.path.exists("/usr/bin/slim"):
            self.desktop_manager = 'slim'

        # Setup sddm
        if os.path.exists("/usr/bin/sddm"):
            self.desktop_manager = 'sddm'

        # setup lightdm
        if os.path.exists("{0}/usr/bin/lightdm".format(DEST_DIR)):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment is not None:
                os.system("sed -i -e 's/^.*user-session=.*/user-session={0}/' \
		{1}/etc/lightdm/lightdm.conf".format(default_desktop_environment.desktop_file, DEST_DIR))
                os.system("ln -s /usr/lib/lightdm/lightdm/gdmflexiserver {0}/usr/bin/gdmflexiserver".format(DEST_DIR))
            os.system("chmod +r {0}/etc/lightdm/lightdm.conf".format(DEST_DIR))
            self.desktop_manager = 'lightdm'

        # Setup gdm
        if os.path.exists("{0}/usr/bin/gdm".format(DEST_DIR)):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment is not None:
                os.system("echo \"XSession={0}\" >> \
                {1}/var/lib/AccountsService/users/gdm".format(default_desktop_environment.desktop_file, DEST_DIR))
                os.system("echo \"Icon=\" >> {0}/var/lib/AccountsService/users/gdm".format(DEST_DIR))
            self.desktop_manager = 'gdm'

        # Setup mdm
        if os.path.exists("{0}/usr/bin/mdm".format(DEST_DIR)):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment is not None:
                os.system("sed -i 's|default.desktop|{0}.desktop|g' \
                {1}/etc/mdm/custom.conf".format(default_desktop_environment.desktop_file, DEST_DIR))
            self.desktop_manager = 'mdm'

        # Setup lxdm
        if os.path.exists("{0}/usr/bin/lxdm".format(DEST_DIR)):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment is not None:
                os.system("sed -i -e 's|^.*session=.*|session={0}|' \
                {1}/etc/lxdm/lxdm.conf".format(default_desktop_environment.executable, DEST_DIR))
            self.desktop_manager = 'lxdm'

        # Setup kdm
        if os.path.exists("{0}/usr/bin/kdm".format(DEST_DIR)):
            self.desktop_manager = 'kdm'

        self.queue_event('info', _("Configure System ..."))

        # Add BROWSER var
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> {0}/etc/environment".format(DEST_DIR))
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> {0}/etc/skel/.bashrc".format(DEST_DIR))
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> {0}/etc/profile".format(DEST_DIR))
        # Add TERM var
        if os.path.exists("{0}/usr/bin/mate-session".format(DEST_DIR)):
            os.system("echo \"TERM=mate-terminal\" >> {0}/etc/environment".format(DEST_DIR))
            os.system("echo \"TERM=mate-terminal\" >> {0}/etc/profile".format(DEST_DIR))

        # Adjust Steam-Native when libudev.so.0 is available
        if (os.path.exists("{0}/usr/lib/libudev.so.0".format(DEST_DIR)) or
                os.path.exists("{0}/usr/lib32/libudev.so.0".format(DEST_DIR))):
            os.system("echo -e \"STEAM_RUNTIME=0\nSTEAM_FRAME_FORCE_CLOSE=1\" >> {0}/etc/environment".format(DEST_DIR))

        # Remove thus
        if os.path.exists("{0}/usr/bin/thus".format(DEST_DIR)):
            self.queue_event('info', _("Removing live configuration (packages)"))
            chroot_run(['pacman', '-R', '--noconfirm', 'thus'])

        # Remove virtualbox driver on real hardware
        p1 = subprocess.Popen(["mhwd"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep", "0300:80ee:beef"], stdin=p1.stdout, stdout=subprocess.PIPE)
        num_res = p2.communicate()[0]
        if num_res == "0":
            chroot_run(['sh', '-c', 'pacman -Rsc --noconfirm $(pacman -Qq | grep virtualbox-guest-modules)'])

        # Set unique machine-id
        chroot_run(['dbus-uuidgen', '--ensure=/etc/machine-id'])
        chroot_run(['dbus-uuidgen', '--ensure=/var/lib/dbus/machine-id'])

        # Setup pacman
        self.queue_event("action", _("Configuring package manager"))

        # Copy mirror list
        shutil.copy2('/etc/pacman.d/mirrorlist',
                     os.path.join(DEST_DIR, 'etc/pacman.d/mirrorlist'))

        # Copy random generated keys by pacman-init to target
        if os.path.exists("{0}/etc/pacman.d/gnupg".format(DEST_DIR)):
            os.system("rm -rf {0}/etc/pacman.d/gnupg".format(DEST_DIR))
        os.system("cp -a /etc/pacman.d/gnupg {0}/etc/pacman.d/".format(DEST_DIR))
        chroot_run(['pacman-key', '--populate', 'archlinux', 'manjaro'])
        self.queue_event('info', _("Finished configuring package manager."))

        # Let's start without using hwdetect for mkinitcpio.conf.
        # I think it should work out of the box most of the time.
        # This way we don't have to fix deprecated hooks.
        # NOTE: With LUKS or LVM maybe we'll have to fix deprecated hooks.
        self.queue_event('info', _("Running mkinitcpio ..."))
        mkinitcpio.run(DEST_DIR, self.settings, self.mount_devices, self.blvm)
        self.queue_event('info', _("Running mkinitcpio - done"))

        # Set autologin if selected
        # In openbox "desktop", the post-install script writes /etc/slim.conf
        # so we always have to call set_autologin AFTER the post-install script.
        if self.settings.get('require_password') is False:
            self.set_autologin()

        # Encrypt user's home directory if requested
        # FIXME: This is not working atm
        if self.settings.get('encrypt_home'):
            logging.debug(_("Encrypting user home dir..."))
            encfs.setup(username, DEST_DIR)
            logging.debug(_("User home dir encrypted"))

        # Install boot loader (always after running mkinitcpio)
        if self.settings.get('bootloader_install'):
            try:
                self.queue_event('info', _("Installing bootloader..."))
                from installation import bootloader

                boot_loader = bootloader.Bootloader(DEST_DIR,
                                                    self.settings,
                                                    self.mount_devices)
                boot_loader.install()
            except Exception as error:
                logging.error(_("Couldn't install boot loader: {0}"
                                .format(error)))
      
        self.queue_event('pulse', 'stop')        
        chroot.umount_special_dirs(DEST_DIR)
