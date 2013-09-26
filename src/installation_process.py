#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  installation_process.py
#  
#  Copyright 2013 Antergos, Manjaro
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
#  Antergos Team:
#   Alex Filgueira (faidoc) <alexfilgueira.antergos.com>
#   Raúl Granados (pollitux) <raulgranados.antergos.com>
#   Gustau Castells (karasu) <karasu.antergos.com>
#   Kirill Omelchenko (omelcheck) <omelchek.antergos.com>
#   Marc Miralles (arcnexus) <arcnexus.antergos.com>
#   Alex Skinner (skinner) <skinner.antergos.com>
#
#  Manjaro Team:
#   Philip Müller (philm) <philm.manjaro.org>
#   Mateusz Mikołajczyk (toudi) <mikolajczyk.mateusz.gmail.com>
#

import multiprocessing
import queue

import subprocess
import os
import sys
import time
import shutil
import xml.etree.ElementTree as etree
import urllib.request
import urllib.error
import crypt
import download
import config
import logging
import info

from configobj import ConfigObj
import stat
import math
#import commands

## BEGIN: RSYNC-based file copy support
#CMD = 'unsquashfs -f -i -da 32 -fr 32 -d %(dest)s %(source)s'
CMD = 'rsync -ar --progress %(source)s %(dest)s'
from threading import Thread
from queue import Queue, Empty
import re
ON_POSIX = 'posix' in sys.builtin_module_names

q = Queue()

def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        queue.put(line)
    out.close()

# Update the value of the progress bar so that we get
# some movement
class FileCopyThread(Thread):
    def __init__(self, installer, current_file, total_files, source, dest):
        self.our_current = current_file
        self.process = subprocess.Popen(
            (CMD % {
                'source': source,
                'dest': dest,
            }).split(),
            bufsize=1,
            stdout=subprocess.PIPE,
            close_fds=ON_POSIX
        )
        self.installer = installer
        self.total_files = total_files
        t = Thread(target=enqueue_output, args=(self.process.stdout, q))
        t.daemon = True # thread dies with the program
        t.start()
        super(FileCopyThread, self).__init__()

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()

    def update_label(self, text):
        self.installer.queue_event('info', _("Copying '/%s'" % text))

    def update_progress(self, our_current):
        progress = (float(our_current)/float(self.total_files))
        self.installer.queue_event('percent', progress)

    def run(self):
        while self.process.poll() is None:
            try:
                line = q.get(timeout=3)
            except Empty:
                continue
            else:
                #self.update_label(line.strip())
                #self.our_current += 1
                #self.update_progress(self.our_current)
                if 'to-check' in line.decode():
                    # we've got a percentage update
                    self.our_current += 1
                    self.update_progress(self.our_current)
                else:
                    # we've got a filename!
                    self.update_label(line.decode().strip())

## END: RSYNC-based file copy support

conf_file = '/etc/live-installer/install.conf'
configuration = ConfigObj(conf_file)

# Insert the src/pacman directory at the front of the path.
base_dir = os.path.dirname(__file__) or '.'
pacman_dir = os.path.join(base_dir, 'pacman')
sys.path.insert(0, pacman_dir)

# Insert the src/parted directory at the front of the path.
base_dir = os.path.dirname(__file__) or '.'
parted_dir = os.path.join(base_dir, 'parted')
sys.path.insert(0, parted_dir)

import fs_module as fs
import misc
import pac

_autopartition_script = 'auto_partition.sh'
_postinstall_script = 'postinstall.sh'
_mhwd_script = 'mhwd.sh'

class InstallError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class InstallationProcess(multiprocessing.Process):
    def __init__(self, settings, callback_queue, mount_devices, \
                 fs_devices, ssd=None, alternate_package_list="", blvm=False):
        multiprocessing.Process.__init__(self)
                
        self.alternate_package_list = alternate_package_list
        
        self.callback_queue = callback_queue
        self.settings = settings
        self.blvm = blvm
        self.method = self.settings.get('partition_mode')
        
        self.queue_event('info', _("Installing using the '%s' method") % self.method)
        
        self.ssd = ssd
        self.mount_devices = mount_devices
        
        # Check desktop selected to load packages needed
        self.desktop = self.settings.get('desktop')
        #self.desktop_manager = 'gdm'
        self.network_manager = 'NetworkManager'
        self.card = []
        # Packages to be removed
        self.conflicts = []

        self.fs_devices = fs_devices
        
        self.running = True
        self.error = False

    def has_connection(self):
        import dbus
        try:
            bus = dbus.SystemBus()
            manager = bus.get_object(NM, '/org/freedesktop/NetworkManager')
            state = self.get_prop(manager, NM, 'state')
        except dbus.exceptions.DBusException:
            logging.warning(_("In installation-process, can't get network status"))
            return False
        return state == NM_STATE_CONNECTED_GLOBAL
    
    def queue_fatal_event(self, txt):
        # Queue the fatal event and exit process
        self.error = True
        self.running = False
        self.queue_event('error', txt)
        self.callback_queue.join()
        sys.exit(1)
         
    def queue_event(self, event_type, event_text=""):
        try:
            self.callback_queue.put_nowait((event_type, event_text))
        except queue.Full:
            pass
            
    @misc.raise_privileges    
    def run(self):
        p = multiprocessing.current_process()
        #log.debug("Starting: [%d] %s" % (p.pid, p.name))
        
        # Common vars
        self.packages = []
        
        self.dest_dir = "/install"
        if not os.path.exists(self.dest_dir):
            os.makedirs(self.dest_dir)
        else:
            # If we're recovering from a failed/stoped install, there'll be
            # some mounted directories. Try to unmount them first
        
            install_dirs = { "boot", "dev", "proc", "sys", "var" }
            for p in install_dirs:
                p = os.path.join(self.dest_dir, p)
                (fsname, fstype, writable) = misc.mount_info(p)
                if fsname:
                    subprocess.check_call(['umount', p])
                    self.queue_event('debug', "%s unmounted" % p)
            # now we can unmount /install
            (fsname, fstype, writable) = misc.mount_info(self.dest_dir)
            if fsname:
                subprocess.check_call(['umount', self.dest_dir])
                self.queue_event('debug', "%s unmounted" % self.dest_dir)

        # get settings
        self.distribution_name = configuration['distribution']['DISTRIBUTION_NAME']
        self.distribution_version = configuration['distribution']['DISTRIBUTION_VERSION']        
        self.live_user = configuration['install']['LIVE_USER_NAME']
        self.media = configuration['install']['LIVE_MEDIA_SOURCE']
        self.media_desktop = configuration['install']['LIVE_MEDIA_DESKTOP']
        self.media_type = configuration['install']['LIVE_MEDIA_TYPE']
        self.kernel = configuration['install']['KERNEL']

        self.vmlinuz = "vmlinuz-%s" % self.kernel
        self.initramfs = "initramfs-%s" % self.kernel   

        self.arch = os.uname()[-1]
                
        ## Create/Format partitions
        
        if self.method == 'automatic':
            self.auto_device = self.mount_devices["/"].replace("3","")
            thus_dir = self.settings.get("THUS_DIR")
            script_path = os.path.join(thus_dir, "scripts", _autopartition_script)
            try:
                self.queue_event('debug', "Automatic device: %s" % self.auto_device)
                self.queue_event('debug', "Running automatic script...")
                self.queue_event('pulse') 
                subprocess.check_call(["/usr/bin/bash", script_path, self.auto_device])
                self.queue_event('stop_pulse') 
                self.queue_event('debug', "Automatic script done.")
            except subprocess.FileNotFoundError as e:
                self.queue_fatal_event(_("Can't execute the auto partition script"))
                return False
            except subprocess.CalledProcessError as e:
                self.queue_fatal_event("CalledProcessError.output = %s" % e.output)
                return False

        if self.method == 'alongside':
            # Alongside method shrinks selected partition
            # and creates root and swap partition in the available space
            boot_partition, root_partition = shrink(self.mount_devices["alongside"])
            # Alongside method formats root by default (as it is always a new partition)
            (error, msg) = fs.create_fs(self.mount_devices["/"], "ext4")
        
        if self.method == 'advanced':
            root_partition = self.mount_devices["/"]
            
            if root_partition in self.fs_devices:
                root_fs = self.fs_devices[root_partition]
            else:
                root_fs = "ext4"
                
            if "/boot" in self.mount_devices:
                boot_partition = self.mount_devices["/boot"]
            else:
                boot_partition = ""

            if "swap" in self.mount_devices:
                swap_partition = self.mount_devices["swap"]
            else:
                swap_partition = ""

            # Advanced method formats root by default in installation_advanced

        # Create the directory where we will mount our new root partition
        if not os.path.exists(self.dest_dir):
            os.mkdir(self.dest_dir)

        # Mount root and boot partitions (only if it's needed)
        if self.method == 'alongside' or self.method == 'advanced':
            # not doing this in automatic mode as our script mounts the root and boot devices
            try:
                txt = _("Mounting partition %s into %s directory") % (root_partition, self.dest_dir)
                self.queue_event('debug', txt)
                subprocess.check_call(['mount', root_partition, self.dest_dir])
                # We also mount the boot partition if it's needed
                subprocess.check_call(['mkdir', '-p', '%s/boot' % self.dest_dir]) 
                if "/boot" in self.mount_devices:
                    txt = _("Mounting partition %s into %s/boot directory") % (boot_partition, self.dest_dir)
                    self.queue_event('debug', txt)
                    subprocess.check_call(['mount', boot_partition, "%s/boot" % self.dest_dir])
            except subprocess.CalledProcessError as e:
                self.queue_fatal_event(_("Couldn't mount root and boot partitions"))
                return False
        
        # In advanced mode, mount all partitions (root and boot are already mounted)
        if self.method == 'advanced':
            for path in self.mount_devices:
                mp = self.mount_devices[path]
                # Root and Boot are already mounted.
                # Just try to mount all the rest.
                if mp != root_partition and mp != boot_partition and mp != swap_partition:
                    try:
                        mount_dir = self.dest_dir + path
                        if not os.path.exists(mount_dir):
                            os.makedirs(mount_dir)
                        txt = _("Mounting partition %s into %s directory") % (mp, mount_dir)
                        self.queue_event('debug', txt)
                        subprocess.check_call(['mount', mp, mount_dir])
                    except subprocess.CalledProcessError as e:
                        # we try to continue as root and boot mounted ok
                        self.queue_event('debug', _("Can't mount %s in %s") % (mp, mount_dir))
                        # self.queue_fatal_event(_("Couldn't mount %s") % mount_dir)
                        # return False


        # If pacman was stoped and /var is in another partition than root
        # (so as to be able to resume install), database lock file will still be in place.
        # We must delete it or this new installation will fail

        db_lock = os.path.join(self.dest_dir, "var/lib/pacman/db.lck")
        if os.path.exists(db_lock):
            with misc.raised_privileges():
                os.remove(db_lock)
            logging.debug("%s deleted" % db_lock)


        # Create some needed folders
        try:
            subprocess.check_call(['mkdir', '-p', '%s/var/lib/pacman' % self.dest_dir])
            subprocess.check_call(['mkdir', '-p', '%s/etc/pacman.d/gnupg/' % self.dest_dir])
            subprocess.check_call(['mkdir', '-p', '%s/var/log/' % self.dest_dir]) 
        except subprocess.CalledProcessError as e:
            self.queue_fatal_event(_("Can't create necessary directories on destination system"))
            return False

        try:
            self.queue_event('debug', 'Install System...')
            # very slow ...
            self.install_system()

            subprocess.check_call(['mkdir', '-p', '%s/var/log/' % self.dest_dir])
            self.queue_event('debug', 'System installed')

            self.queue_event('debug', 'Configuring system...')
            self.configure_system()
            self.queue_event('debug', 'System configured.')

            if self.settings.get('install_bootloader'):
                self.queue_event('debug', 'Installing bootloader...')
                self.install_bootloader()         
        except subprocess.CalledProcessError as e:
            self.queue_fatal_event("CalledProcessError.output = %s" % e.output)
            return False
        except InstallError as e:
            self.queue_fatal_event(e.value)
            return False
        except:
            # unknown error
            self.running = False
            self.error = True
            return False

        # installation finished ok
        self.queue_event("finished")
        self.running = False
        self.error = False
        return True

    def download_packages(self):
        conf_file = "/tmp/pacman.conf"
        cache_dir = "%s/var/cache/pacman/pkg" % self.dest_dir
        download.DownloadPackages(self.packages, conf_file, cache_dir, self.callback_queue)
       
    # Add gnupg pacman files to installed system
    # Needs testing, but it seems to be the way to do it now
    # Must be also changed in the CLI Installer
    def prepare_pacman_keychain(self):
        # removed / from etc to make path relative...
        dest_path = os.path.join(self.dest_dir, "etc/pacman.d/gnupg")
        # use copytree for cp -r
        try:
            misc.copytree('/etc/pacman.d/gnupg', dest_path)
        except (FileExistsError, shutil.Error) as e:
            # log error but continue anyway
            logging.exception(e)

    # Configures pacman and syncs db on destination system
    def prepare_pacman(self):
        dirs = [ "var/cache/pacman/pkg", "var/lib/pacman" ]
        
        for d in dirs:
            mydir = os.path.join(self.dest_dir, d)
            if not os.path.exists(mydir):
                os.makedirs(mydir)

        self.prepare_pacman_keychain()
        
        self.pac.do_refresh()

    # Copies all files to target
    def install_system(self):
        # mount the media location.
        try:
            if(not os.path.exists(self.dest_dir)):
                os.mkdir(self.dest_dir)
            if(not os.path.exists("/source")):
                os.mkdir("/source")
            if(not os.path.exists("/source_desktop")):
                os.mkdir("/source_desktop")
            # find the squashfs..
            if(not os.path.exists(self.media)):
                self.queue_event('debug', "Base filesystem does not exist! Critical error (exiting).")
                sys.exit(1) # change to report
            if(not os.path.exists(self.media_desktop)):
                self.queue_event('debug', "Desktop filesystem does not exist! Critical error (exiting).")
                sys.exit(1) # change to report

            # Mount the installation media
            subprocess.check_call(["mount", self.media, "/source/", "-t", self.media_type, "-o", "loop"])
            subprocess.check_call(["mount", self.media_desktop, "/source_desktop/", "-t", self.media_type, "-o", "loop"])

            # walk root filesystem
            SOURCE = "/source/"
            DEST = self.dest_dir
            directory_times = []
            # index the files
            self.queue_event('info', "Indexing files to be copied..")
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc","-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output1 = p2.communicate()[0]
            self.queue_event('info', "Indexing files to be copied..")
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media_desktop], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc","-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output2 = p2.communicate()[0]	
            our_total = int(float(output1)+float(output2))
            self.queue_event('info', "Extracting root-image..")
            our_current = 0
            #t = FileCopyThread(self, our_total, self.media, DEST)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST)
            t.start()
            t.join()
            # walk desktop filesystem
            SOURCE = "/source_desktop/"
            DEST = self.dest_dir
            directory_times = []
            self.queue_event('info', "Extracting desktop-image..")
            our_current = int(output1)
            #t = FileCopyThread(self, our_total, self.media_desktop, DEST)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST)
            t.start()
            t.join()
            for dirtime in directory_times:
                (directory, atime, mtime) = dirtime
                try:
                    self.queue_event('info', _("Restoring meta-information on %s" % directory))
                    os.utime(directory, (atime, mtime))
                except OSError:
                    pass

        except Exception:
            import traceback
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)
   
    def chroot_mount(self):
        dirs = [ "sys", "proc", "dev" ]
        for d in dirs:
            mydir = os.path.join(self.dest_dir, d)
            if not os.path.exists(mydir):
                os.makedirs(mydir)

        mydir = os.path.join(self.dest_dir, "sys")

        subprocess.check_call(["mount", "-t", "sysfs", "sysfs", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        mydir = os.path.join(self.dest_dir, "proc")
        subprocess.check_call(["mount", "-t", "proc", "proc", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        mydir = os.path.join(self.dest_dir, "dev")
        subprocess.check_call(["mount", "-o", "bind", "/dev", mydir])
        
    def chroot_umount(self):
        dirs = [ "proc", "sys", "dev" ]
        
        for d in dirs:
            mydir = os.path.join(self.dest_dir, d)
            subprocess.check_call(["umount", mydir])

    def chroot(self, cmd, stdin=None, stdout=None):
        run = ['chroot', self.dest_dir]
        
        for c in cmd:
            run.append(c)
      
        try:
            proc = subprocess.Popen(run,
                                    stdin=stdin,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            out = proc.communicate()[0]
        except OSError as e:
            logging.exception("Error running command: %s" % e.strerror)
            raise
        
        
    def is_running(self):
        return self.running

    def is_ok(self):
        return not self.error

    def copy_network_config(self):
        source_nm = "/etc/NetworkManager/system-connections/"
        target_nm = "%s/etc/NetworkManager/system-connections/" % self.dest_dir

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

                shutil.copy(source_network, target_network)

    # TODO: Take care of swap partitions
    def auto_fstab(self):
        all_lines = []
        all_lines.append("# /etc/fstab: static file system information.")
        all_lines.append("#")
        all_lines.append("# Created by Manjaro Live-Installer")
        all_lines.append("#")
        all_lines.append("# Use 'blkid' to print the universally unique identifier for a")
        all_lines.append("# device; this may be used with UUID= as a more robust way to name devices")
        all_lines.append("# that works even if disks are added and removed. See fstab(5).")
        all_lines.append("#")
        all_lines.append("# <file system> <mount point>   <type>  <options>       <dump>  <pass>")
        all_lines.append("#")

        root_ssd = 0

        for path in self.mount_devices:
            opts = 'defaults'
            chk = '0'
            parti = self.mount_devices[path]
            info = fs.get_info(parti)
            uuid = info['UUID']
            if parti in self.fs_devices:
                myfmt = self.fs_devices[parti]
            else:
                # It hasn't any filesystem defined (maybe swap?)
                # TODO: don't skip it and mount it as it should be if it is a swap partition
                continue

            if path == '/':
                chk = '1'
                opts = "rw,relatime,data=ordered"
            else:
                full_path = os.path.join(self.dest_dir, path)
                subprocess.check_call(["mkdir", "-p", full_path])

            if self.ssd != None:
                for i in self.ssd:
                    if i in self.mount_devices[path] and self.ssd[i]:
                        opts = 'defaults,noatime,nodiratime'
                        # As of linux kernel version 3.7, the following
                        # filesystems support TRIM: ext4, btrfs, JFS, and XFS.
                        # If using a TRIM supported SSD, discard is a valid mount option for swap
                        if myfmt == 'ext4' or myfmt == 'btrfs' or myfmt == 'jfs' or myfmt == 'xfs' or myfmt == 'swap':
                            opts += ',discard'
                        if path == '/':
                            root_ssd = 1

            all_lines.append("UUID=%s %s %s %s 0 %s" % (uuid, path, myfmt, opts, chk))

        if root_ssd:
            all_lines.append("tmpfs /tmp tmpfs defaults,noatime,mode=1777 0 0")

        full_text = '\n'.join(all_lines)

        with open('/install/etc/fstab','w') as f:
            f.write(full_text)

    def install_bootloader(self):
        # TODO: check dogrub_config and dogrub_bios from arch-setup
        
        bt = self.settings.get('bootloader_type')

        if bt == "GRUB2":
            self.install_bootloader_grub2_bios()
        elif bt == "UEFI_x86_64" or bt == "UEFI_i386":
            self.install_bootloader_grub2_efi(bt)
    
    def install_bootloader_grub2_bios(self):
        grub_device = self.settings.get('bootloader_device')
        self.queue_event('info', _("Installing GRUB(2) BIOS boot loader in %s") % grub_device)

        self.chroot_mount()

        self.chroot(['grub-install', \
                  '--directory=/usr/lib/grub/i386-pc', \
                  '--target=i386-pc', \
                  '--boot-directory=/boot', \
                  '--recheck', \
                  grub_device])
        
        grub_d_dir = os.path.join(self.dest_dir, "etc/grub.d")
        
        if not os.path.exists(grub_d_dir):
            os.makedirs(grub_d_dir)

        locale = self.settings.get("locale")
        self.chroot(['sh', '-c', 'LANG=%s grub-mkconfig -o /boot/grub/grub.cfg' % locale])
        
        self.chroot_umount()

        core_path = os.path.join(self.dest_dir, "boot/grub/i386-pc/core.img")
        
        if os.path.exists(core_path):
            self.queue_event('info', _("GRUB(2) BIOS has been successfully installed."))
        else:
            self.queue_event('warning', _("ERROR installing GRUB(2) BIOS."))

    def install_bootloader_grub2_efi(self, arch):
        uefi_arch = "x86_64"
        spec_uefi_arch = "x64"
        
        if bt == "UEFI_i386":
            uefi_arch = "i386"
            spec_uefi_arch = "ia32"

        grub_device = self.settings.get('bootloader_device')
        self.queue_event('info', _("Installing GRUB(2) UEFI %s boot loader in %s") % (uefi_arch, grub_device))

        self.chroot_mount()
        
        self.chroot(['grub-install', \
                  '--directory=/usr/lib/grub/%s-efi' % uefi_arch, \
                  '--target=%s-efi' % uefi_arch, \
                  '--bootloader-id="manjaro_grub"', \
                  '--boot-directory=/boot', \
                  '--recheck', \
                  grub_device])
        
        dest_locale_dir = os.path.join(self.dest_dir, "boot/grub/locale")
        
        if not os.path.exists(dest_locale_dir):
            os.makedirs(dest_locale_dir)
        
        mo = os.path.join(self.dest_dir, "usr/share/locale/en@quot/LC_MESSAGES/grub.mo")

        try:
            shutil.copy2(mo, os.path.join(dest_locale_dir, "en.mo"))
        except FileNotFoundError:
            self.chroot_umount()            
            self.queue_event('warning', _("ERROR installing GRUB(2) UEFI."))
            return
        except FileExistsError:
            # ignore if already exists
            pass

        d = self.dest_dir
        locale = self.settings.get("locale")
        self.chroot(['sh', '-c', 'LANG=%s grub-mkconfig -o /boot/grub/grub.cfg' % locale])
        
        self.chroot_umount()

        grub_cfg = "%s/boot/grub/grub.cfg" % d
        grub_standalone = "%s/boot/efi/EFI/manjaro_grub/grub%s_standalone.cfg" % (d, spec_uefi_arch)
        try:
            shutil.copy2(grub_cfg, grub_standalone)
        except FileNotFoundError:
            self.chroot_umount()            
            self.queue_event('warning', _("ERROR installing GRUB(2) UEFI."))
            return
        except FileExistsError:
            # ignore if already exists
            pass

        self.chroot(['grub-mkstandalone', \
                  '--directory=/usr/lib/grub/%s-efi' % uefi_arch, \
                  '--format=%s-efi' % uefi_arch, \
                  '--compression="xz"', \
                  '--output="/boot/efi/EFI/manjaro_grub/grub%s_standalone.efi' % spec_uefi_arch, \
                  'boot/grub/grub.cfg'])      

    def enable_services(self, services):
        for name in services:
            name += '.service'
            self.chroot(['systemctl', 'enable', name])

    def change_user_password(self, user, new_password):
        try:
            shadow_password = crypt.crypt(new_password,"$6$%s$" % user)
        except:
            self.queue_event('warning', _('Error creating password hash for user %s') % user)
            return False

        try:
            self.chroot(['usermod', '-p', shadow_password, user])
        except:
            self.queue_event('warning', _('Error changing password for user %s') % user)
            return False
        
        return True

    def auto_timesetting(self):
        subprocess.check_call(["hwclock", "--systohc", "--utc"])
        shutil.copy2("/etc/adjtime", "%s/etc/" % self.dest_dir)

    # runs mkinitcpio on the target system
    def run_mkinitcpio(self):
        if self.blvm:
            with open("%s/etc/mkinitcpio.conf" % self.dest_dir) as f:
                mklins = [x.strip() for x in f.readlines()]
            for e in range(len(mklins)):
                if mklins[e].startswith("HOOKS"):
                   mklins[e] = mklins[e].strip('"') + ' lvm2"'
            with open("%s/etc/mkinitcpio.conf" % self.dest_dir, "w") as f:
                f.write("\n".join(mklins) + "\n")
        self.chroot_mount()
        self.chroot(["/usr/bin/mkinitcpio", "-p", self.kernel])
        self.chroot_umount()

    # Uncomment selected locale in /etc/locale.gen
    def uncomment_locale_gen(self, locale):
        #self.chroot(['sed', '-i', '-r', '"s/#(.*%s)/\1/g"' % locale, "/etc/locale.gen"])
            
        text = []
        with open("%s/etc/locale.gen" % self.dest_dir, "rt") as gen:
            text = gen.readlines()
        
        with open("%s/etc/locale.gen" % self.dest_dir, "wt") as gen:
            for line in text:
                if locale in line and line[0] == "#":
                    # uncomment line
                    line = line[1:]
                gen.write(line)

    def encrypt_home(self):
        # TODO: ecryptfs-utils, rsync and lsof packages are needed.
        # They should be added in the livecd AND in the "to install packages" xml list
        
        # Load ecryptfs module
        subprocess.check_call(['modprobe', 'ecryptfs'])
        
        # Get the username and passwd
        username = self.settings.get('username')
        passwd = self.settings.get('password')
        
        # Migrate user home directory
        command = "LOGINPASS=%s ecryptfs-migrate-home -u %s" % (passwd, username)
        outp = subprocess.check_output(command)
        
        with open(os.path.join(self.dest_dir, "tmp/thus-ecryptfs.log", "wt")) as f:
            f.write(outp)
                
        subprocess.check_call(['su', username])
        
        # User should run ecryptfs-unwrap-passphrase and write down the generated passphrase

    # TODO: remove this backport from live-installer

    def do_run_in_chroot(self, command):
        cmd = 'chroot /install/ /bin/sh -c "' + command + '"'
        if '"' in command:
            cmd = "chroot /install/ /bin/sh -c '" + command + "'"
        os.system(cmd)        
                        
    def configure_system(self):
        # final install steps
        # set clock, language, timezone
        # run mkinitcpio
        # populate pacman keyring
        # setup systemd services
        # ... check configure_system from arch-setup

        # Generate the fstab file        
        self.auto_fstab()
        
        # Copy configured networks in Live medium to target system
        if self.network_manager == 'NetworkManager':
            self.copy_network_config()    

        self.queue_event("action", _("Configuring your new system"))
        self.queue_event('pulse') 

        # enable services      
        self.enable_services([ self.network_manager, 'cups', 'dcron', 'remote-fs.target' ])

        # TODO: we never ask the user about this...
        if self.settings.get("use_ntp"):
            self.enable_services(["ntpd"])

        # Wait FOREVER until the user sets the timezone
        while self.settings.get('timezone_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        # set timezone
        zoneinfo_path = os.path.join("/usr/share/zoneinfo", \
                                     self.settings.get("timezone_zone"))
        self.chroot(['ln', '-s', zoneinfo_path, "/etc/localtime"])
        
        # Wait FOREVER until the user sets his params
        while self.settings.get('user_info_done') is False:
            # wait five seconds and try again
            time.sleep(5)         

        # Set user parameters
        username = self.settings.get('username')
        fullname = self.settings.get('fullname')
        password = self.settings.get('password')
        hostname = self.settings.get('hostname')
        
        sudoers_path = os.path.join(self.dest_dir, \
                                    "etc/sudoers.d/10-installer")
        with open(sudoers_path, "wt") as sudoers:
            sudoers.write('%s ALL=(ALL) ALL\n' % username)
        
        subprocess.check_call(["chmod", "440", sudoers_path])
        
        
        self.chroot(['useradd', '-m', '-s', '/bin/bash', \
                  '-g', 'users', '-G', 'lp,video,network,storage,wheel,audio', \
                  username])

        self.change_user_password(username, password)

        self.chroot(['chfn', '-f', fullname, username])

        self.chroot(['chown', '-R', '%s:users' % username, "/home/%s" % username])
        
        hostname_path = os.path.join(self.dest_dir, "etc/hostname")
        if not os.path.exists(hostname_path):
            with open(hostname_path, "wt") as f:
                f.write(hostname)
        
        # User password is the root password  
        self.change_user_password('root', password)

        ## Generate locales
        keyboard_layout = self.settings.get("keyboard_layout")
        keyboard_variant = self.settings.get("keyboard_variant")
        locale = self.settings.get("locale")
        self.queue_event('info', _("Generating locales..."))
        
        self.uncomment_locale_gen(locale)
        
        self.chroot(['locale-gen'])
        locale_conf_path = os.path.join(self.dest_dir, "etc/locale.conf")
        with open(locale_conf_path, "wt") as locale_conf:
            locale_conf.write('LANG=%s \n' % locale)
            locale_conf.write('LC_MESSAGES=%s \n' % locale)
            locale_conf.write('LC_COLLATE=C \n')

        environment_path = os.path.join(self.dest_dir, "etc/environment")
        with open(environment_path, "wt") as environment:
            environment.write('LANG=%s \n' % locale)
            
        # Set /etc/vconsole.conf
        vconsole_conf_path = os.path.join(self.dest_dir, "etc/vconsole.conf")
        with open(vconsole_conf_path, "wt") as vconsole_conf:
            vconsole_conf.write('KEYMAP=%s \n' % keyboard_layout)

        self.queue_event('info', _("Adjusting hardware clock..."))
        self.auto_timesetting()

        # install configs for root
        os.system("cp -a /install/etc/skel/. /install/root/")

        self.queue_event('info', _("Configuring hardware.."))  
        # copy generated xorg.xonf to target
        if os.path.exists("/etc/X11/xorg.conf"):
            os.system("cp /etc/X11/xorg.conf /install/etc/X11/xorg.conf")

        # Install xf86-video driver
        if os.path.exists("/opt/manjaro/pacman-gfx.conf"):
            self.queue_event('info', _("Setup graphic card.."))
            self.queue_event('pulse') 
            mhwd_script_path = os.path.join(self.settings.get("THUS_DIR"), "scripts", _mhwd_script)  
            try:
                self.queue_event('debug', "Running MHWD script...")
                subprocess.check_call(["/usr/bin/bash", mhwd_script_path])
                self.queue_event('debug', "MHWD script done.")
            except subprocess.FileNotFoundError as e:
                self.queue_fatal_event(_("Can't execute the MHWD script"))
                return False
            except subprocess.CalledProcessError as e:
                self.queue_fatal_event("CalledProcessError.output = %s" % e.output)
                return False

        self.queue_event('info', _("Configure display manager..")) 
        # setup lightdm
        if os.path.exists("/usr/bin/lightdm"):
            os.system("mkdir -p /install/run/lightdm")
            self.do_run_in_chroot("getent group lightdm")
            self.do_run_in_chroot("groupadd -g 620 lightdm")
            self.do_run_in_chroot("getent passwd lightdm")
            self.do_run_in_chroot("useradd -c 'LightDM Display Manager' -u 620 -g lightdm -d /var/run/lightdm -s /usr/bin/nologin lightdm")
            self.do_run_in_chroot("passwd -l lightdm")
            os.system("chown -R lightdm:lightdm /install/run/lightdm")
            if os.path.exists("/usr/bin/startxfce4"):
                os.system("sed -i -e 's/^.*user-session=.*/user-session=xfce/' /install/etc/lightdm/lightdm.conf")
                os.system("ln -s /usr/lib/lightdm/lightdm/gdmflexiserver /install/usr/bin/gdmflexiserver")
            os.system("chmod +r /install/etc/lightdm/lightdm.conf")
            self.desktop_manager = 'lightdm'

        # setup gdm
        if os.path.exists("/usr/bin/gdm"):
            self.do_run_in_chroot("getent group gdm")
            self.do_run_in_chroot("groupadd -g 120 gdm")
            self.do_run_in_chroot("getent passwd gdm")
            self.do_run_in_chroot("useradd -c 'Gnome Display Manager' -u 120 -g gdm -d /var/lib/gdm -s /usr/bin/nologin gdm")
            self.do_run_in_chroot("passwd -l gdm")
            self.do_run_in_chroot("chown -R gdm:gdm /var/lib/gdm")
            if os.path.exists("/install/var/lib/AccountsService/users"):
                os.system("echo \"[User]\" > /install/var/lib/AccountsService/users/gdm")
                if os.path.exists("/usr/bin/startxfce4"):
                    os.system("echo \"XSession=xfce\" >> /install/var/lib/AccountsService/users/gdm")
                elif os.path.exists("/usr/bin/cinnamon"):
                    os.system("echo \"XSession=cinnamon\" >> /install/var/lib/AccountsService/users/gdm")
                elif os.path.exists("/usr/bin/mate-session"):
                    os.system("echo \"XSession=mate\" >> /install/var/lib/AccountsService/users/gdm")
                elif os.path.exists("/usr/bin/enlightenment_start"):
                    os.system("echo \"XSession=enlightenment\" >> /install/var/lib/AccountsService/users/gdm")
                elif os.path.exists("/usr/bin/openbox-session"):
                    os.system("echo \"XSession=openbox\" >> /install/var/lib/AccountsService/users/gdm")
                elif os.path.exists("/usr/bin/lxsession"):
                    os.system("echo \"XSession=LXDE\" >> /install/var/lib/AccountsService/users/gdm")
                os.system("echo \"Icon=\" >> /install/var/lib/AccountsService/users/gdm")
            self.desktop_manager = 'gdm'

        # setup mdm
        if os.path.exists("/usr/bin/mdm"):
            self.do_run_in_chroot("getent group mdm")
            self.do_run_in_chroot("groupadd -g 128 mdm")
            self.do_run_in_chroot("getent passwd mdm")
            self.do_run_in_chroot("useradd -c 'Linux Mint Display Manager' -u 128 -g mdm -d /var/lib/mdm -s /usr/bin/nologin mdm")
            self.do_run_in_chroot("passwd -l mdm")
            self.do_run_in_chroot("chown root:mdm /var/lib/mdm")
            self.do_run_in_chroot("chmod 1770 /var/lib/mdm")
            if os.path.exists("/usr/bin/startxfce4"):
                os.system("sed -i 's|default.desktop|xfce.desktop|g' /install/etc/mdm/custom.conf")
            if os.path.exists("/usr/bin/gnome-session-cinnamon"):
                os.system("sed -i 's|default.desktop|cinnamon.desktop|g' /install/etc/mdm/custom.conf")
            if os.path.exists("/usr/bin/openbox-session"):
                os.system("sed -i 's|default.desktop|openbox.desktop|g' /install/etc/mdm/custom.conf")
            if os.path.exists("/usr/bin/mate-session"):
                os.system("sed -i 's|default.desktop|mate.desktop|g' /install/etc/mdm/custom.conf")
            if os.path.exists("/usr/bin/lxsession"):
                os.system("sed -i 's|default.desktop|LXDE.desktop|g' /install/etc/mdm/custom.conf")
            if os.path.exists("/usr/bin/enlightenment_start"):
                os.system("sed -i 's|default.desktop|enlightenment.desktop|g' /install/etc/mdm/custom.conf")
            self.desktop_manager = 'mdm'

        # setup lxdm
        if os.path.exists("/usr/bin/lxdm"):
            self.do_run_in_chroot("groupadd --system lxdm")
            if os.path.exists("/usr/bin/startxfce4"):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/startxfce4|' /install/etc/lxdm/lxdm.conf")
            elif os.path.exists("/usr/bin/cinnamon"):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/cinnamon|' /install/etc/lxdm/lxdm.conf")
            elif os.path.exists("/usr/bin/mate-session"):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/mate-session|' /install/etc/lxdm/lxdm.conf")
            elif os.path.exists("/usr/bin/enlightenment_start"):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/enlightenment_start|' /install/etc/lxdm/lxdm.conf")
            elif os.path.exists("/usr/bin/openbox-session"):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/openbox-session|' /install/etc/lxdm/lxdm.conf")
            elif os.path.exists("/usr/bin/lxsession"):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/lxsession|' /install/etc/lxdm/lxdm.conf")
            os.system("chgrp -R lxdm /install/var/lib/lxdm")
            os.system("chgrp lxdm /install/etc/lxdm/lxdm.conf")
            os.system("chmod +r /install/etc/lxdm/lxdm.conf")
            self.desktop_manager = 'lxdm'

        # setup kdm
        if os.path.exists("/usr/bin/kdm"):
            self.do_run_in_chroot("getent group kdm")
            self.do_run_in_chroot("groupadd -g 135 kdm")
            self.do_run_in_chroot("getent passwd kdm")
            self.do_run_in_chroot("useradd -u 135 -g kdm -d /var/lib/kdm -s /bin/false -r -M kdm")
            self.do_run_in_chroot("chown -R 135:135 var/lib/kdm")
            self.do_run_in_chroot("xdg-icon-resource forceupdate --theme hicolor")
            self.do_run_in_chroot("update-desktop-database -q")
            self.desktop_manager = 'kdm'

        self.queue_event('info', _("Configure System..")) 

        # add BROWSER var
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> /install/etc/environment")
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> /install/etc/skel/.bashrc")
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> /install/etc/profile")
        # add TERM var
        if os.path.exists("/usr/bin/mate-session"):
            os.system("echo \"TERM=mate-terminal\" >> /install/etc/environment")
            os.system("echo \"TERM=mate-terminal\" >> /install/etc/profile")

        # fix_gnome_apps
        self.do_run_in_chroot("glib-compile-schemas /usr/share/glib-2.0/schemas")
        self.do_run_in_chroot("gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor")
        self.do_run_in_chroot("dconf update")

        if os.path.exists("/usr/bin/gnome-keyring-daemon"):
            self.do_run_in_chroot("setcap cap_ipc_lock=ep /usr/bin/gnome-keyring-daemon")

        # fix_ping_installation
        self.do_run_in_chroot("setcap cap_net_raw=ep /usr/bin/ping")
        self.do_run_in_chroot("setcap cap_net_raw=ep /usr/bin/ping6")

        # remove .manjaro-chroot
        #os.system("rm /install/.manjaro-chroot")
        
        # remove live-initramfs (or w/e)
        self.queue_event('info', _("Removing live configuration (packages)"))
        #self.do_run_in_chroot("pacman -R --noconfirm live-installer")

        # remove virtualbox driver on real hardware
        p1 = subprocess.Popen(["mhwd"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep","0300:80ee:beef"], stdin=p1.stdout, stdout=subprocess.PIPE)
        num_res = p2.communicate()[0]
        if num_res == "0":  
             self.do_run_in_chroot("pacman -Rsc --noconfirm $(pacman -Qq | grep virtualbox-guest-modules)")

        # set unique machine-id
        self.do_run_in_chroot("dbus-uuidgen --ensure=/etc/machine-id")
        self.do_run_in_chroot("dbus-uuidgen --ensure=/var/lib/dbus/machine-id")


        # setup pacman
        self.queue_event("action", _("Configuring package manager"))
        self.queue_event("pulse")

        # Copy important config files to target system
        files = [ "/etc/pacman.conf", "/etc/yaourtrc" ]        
        
        for path in files:
            shutil.copy2(path, os.path.join(self.dest_dir, 'etc/'))

        # copy mirror list
        shutil.copy2('/etc/pacman.d/mirrorlist', \
                    os.path.join(self.dest_dir, 'etc/pacman.d/mirrorlist'))

        # copy random generated keys by pacman-init to target
        if os.path.exists("/install/etc/pacman.d/gnupg"):
            os.system("rm -rf /install/etc/pacman.d/gnupg")
        os.system("cp -a /etc/pacman.d/gnupg /install/etc/pacman.d/")
        self.chroot_mount()
        self.do_run_in_chroot("pacman-key --populate archlinux manjaro")
        if self.has_connection is True:
            self.do_run_in_chroot("pacman -Syy")
        self.chroot_umount()

        self.queue_event('info', _("Finished configuring package manager."))

        #desktop = self.settings.get('desktop')

        desktop = "x"
        
        if desktop != "nox":
            # Set /etc/X11/xorg.conf.d/00-keyboard.conf for the xkblayout
            xorg_conf_xkb_path = os.path.join(self.dest_dir, "etc/X11/xorg.conf.d/00-keyboard.conf")
            with open(xorg_conf_xkb_path, "wt") as xorg_conf_xkb:
                xorg_conf_xkb.write("# Read and parsed by systemd-localed. It's probably wise not to edit this file\n")
                xorg_conf_xkb.write('# manually too freely.\n')
                xorg_conf_xkb.write('Section "InputClass"\n')
                xorg_conf_xkb.write('        Identifier "system-keyboard"\n')
                xorg_conf_xkb.write('        MatchIsKeyboard "on"\n')
                xorg_conf_xkb.write('        Option "XkbLayout" "%s"\n' % keyboard_layout)
                if keyboard_variant != '':
                    xorg_conf_xkb.write('        Option "XkbVariant" "%s"\n' % keyboard_variant)
                xorg_conf_xkb.write('EndSection\n')

            # Set autologin if selected
            if self.settings.get('require_password') is False:
                # Systems with GDM as Desktop Manager
                if self.desktop_manager == 'gdm':
                    gdm_conf_path = os.path.join(self.dest_dir, "etc/gdm/custom.conf")
                    with open(gdm_conf_path, "wt") as gdm_conf:
                        gdm_conf.write('# Enable automatic login for user\n')
                        gdm_conf.write('[daemon]\n')
                        gdm_conf.write('AutomaticLogin=%s\n' % username)
                        gdm_conf.write('AutomaticLoginEnable=True\n')

                # Systems with MDM as Desktop Manager
                if self.desktop_manager == 'mdm':
                    mdm_conf_path = os.path.join(self.dest_dir, "etc/mdm/custom.conf")
                    with open(gdm_conf_path, "wt") as mdm_conf:
                        mdm_conf.write('# Enable automatic login for user\n')
                        mdm_conf.write('[daemon]\n')
                        mdm_conf.write('AutomaticLogin=%s\n' % username)
                        mdm_conf.write('AutomaticLoginEnable=True\n')

                # Systems with KDM as Desktop Manager
                elif self.desktop_manager == 'kdm':
                    kdm_conf_path = os.path.join(self.dest_dir, "usr/share/config/kdm/kdmrc")
                    text = []
                    with open(kdm_conf_path, "rt") as kdm_conf:
                        text = kdm_conf.readlines()
            
                    with open(kdm_conf_path, "wt") as kdm_conf:
                        for line in text:
                            if '#AutoLoginEnable=true' in line:
                                line = '#AutoLoginEnable=true \n'
                                line = line[1:]
                            if 'AutoLoginUser=' in line:
                                line = 'AutoLoginUser=%s \n' % username
                            kdm_conf.write(line)

                # Systems with LXDM as Desktop Manager
                elif self.desktop_manager == 'lxdm':
                    lxdm_conf_path = os.path.join(self.dest_dir, "etc/lxdm/lxdm.conf")
                    text = []
                    with open(lxdm_conf_path, "rt") as lxdm_conf:
                        text = lxdm_conf.readlines()
            
                    with open(lxdm_conf_path, "wt") as lxdm_conf:
                        for line in text:
                            if '# autologin=dgod' in line and line[0] == "#":
                                # uncomment line
                                line = '# autologin=%s' % username
                                line = line[1:]
                            lxdm_conf.write(line)

                # Systems with LightDM as the Desktop Manager
                elif self.desktop_manager == 'lightdm':
                    lightdm_conf_path = os.path.join(self.dest_dir, "etc/lightdm/lightdm.conf")
                    # Ideally, use configparser for the ini conf file, but just do
                    # a simple text replacement for now
                    text = []
                    with open(lightdm_conf_path, "rt") as lightdm_conf:
                        text = lightdm_conf.readlines()

                    with open(lightdm_conf_path, "wt") as lightdm_conf:
                        for line in text:
                            if '#autologin-user=' in line:
                                line = 'autologin-user=%s\n' % username
                            lightdm_conf.write(line)

        # Let's start without using hwdetect for mkinitcpio.conf.
        # I think it should work out of the box most of the time.
        # This way we don't have to fix deprecated hooks.    
        self.queue_event('info', _("Running mkinitcpio..."))
        self.queue_event("pulse")
        self.run_mkinitcpio()
        self.queue_event('info', _("Running mkinitcpio... done"))
        
        '''# Call post-install script to execute gsettings commands
        script_path_postinstall = os.path.join(self.settings.get("THUS_DIR"), \
            "scripts", _postinstall_script)
        subprocess.check_call(["/usr/bin/bash", script_path_postinstall, \
            username, self.dest_dir, self.desktop, keyboard_layout, keyboard_variant])'''

        # In openbox "desktop", the postinstall script writes /etc/slim.conf
        # so we have to modify it here (after running the script).
        # Set autologin if selected
        if self.settings.get('require_password') is False and \
           self.desktop_manager == 'slim':
            slim_conf_path = os.path.join(self.dest_dir, "etc/slim.conf")
            text = []
            with open(slim_conf_path, "rt") as slim_conf:
                text = slim_conf.readlines()
            with open(slim_conf_path, "wt") as slim_conf:
                for line in text:
                    if 'auto_login' in line:
                        line = 'auto_login yes\n'
                    if 'default_user' in line:
                        line = 'default_user %s\n' % username
                    slim_conf.write(line)
              
        '''# Setup ufw if it's an user wanted feature
        if self.settings.get("feature_firewall"):
            pass
            # ufw default deny
            # ufw allow Transmission
            # ufw enable
            # systemctl enable ufw.service'''
			
                
        # encrypt home directory if requested
        if self.settings.get('encrypt_home'):
            self.encrypt_home()

