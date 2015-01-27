#!/usr/bin/env python
#
# ovf.py - Class for OVF/OVA handling
#
# August 2013, Glenn F. Matthews
# Copyright (c) 2013-2015 the COT project developers.
# See the COPYRIGHT.txt file at the top-level directory of this distribution
# and at https://github.com/glennmatthews/cot/blob/master/COPYRIGHT.txt.
#
# This file is part of the Common OVF Tool (COT) project.
# It is subject to the license terms in the LICENSE.txt file found in the
# top-level directory of this distribution and at
# https://github.com/glennmatthews/cot/blob/master/LICENSE.txt. No part
# of COT, including this file, may be copied, modified, propagated, or
# distributed except according to the terms contained in the LICENSE.txt file.

import logging
import os
import os.path
import re
import tarfile
import shutil
import xml.etree.ElementTree as ET
# In 2.7+, ET raises a ParseError if XML parsing fails,
# but in 2.6 it raises an ExpatError. Hide this variation.
try:
    from xml.etree.ElementTree import ParseError
except ImportError:
    from xml.parsers.expat import ExpatError as ParseError
import textwrap

from .xml_file import XML
from .vm_description import VMDescription, VMInitError
from .data_validation import natural_sort, match_or_die, check_for_conflict
from .data_validation import ValueTooHighError, ValueUnsupportedError
from .helper_tools import get_checksum, get_disk_capacity, convert_disk_image
import COT.platforms as Platform
from .cli import TEXT_WIDTH

logger = logging.getLogger(__name__)


def byte_count(base_val, multiplier):
    """Convert an OVF-style value + multiplier into decimal byte count.
    >>> byte_count("128", "byte * 2^20")
    134217728
    """
    match = re.search("2\^(\d+)", multiplier)
    if match:
        return int(base_val) << int(match.group(1))
    return int(base_val)


def factor_bytes(byte_count):
    """Convert a byte count into OVF-style bytes + multiplier.
    >>> factor_bytes(134217728)
    ('128', 'byte * 2^20')
    >>> factor_bytes(134217729)
    ('134217729', 'byte')
    """
    shift = 0
    byte_count = int(byte_count)
    while (byte_count % 1024 == 0):
        shift += 10
        byte_count /= 1024
    byte_count = str(int(byte_count))
    if shift == 0:
        return (byte_count, "byte")
    return (byte_count, "byte * 2^{0}".format(shift))


def byte_string(byte_count, base_shift=0):
    """Pretty-print the given bytes value"""
    tags = ["B", "kB", "MB", "GB", "TB"]
    byte_count = float(byte_count)
    shift = base_shift
    while (byte_count > 1000.0):
        byte_count /= 1024.0
        shift += 1
    if shift == base_shift:
        return "{0} {1}".format(int(byte_count), tags[shift])
    else:
        return "{0:0.2f} {1}".format(byte_count, tags[shift])


class OVF(VMDescription, XML):
    """Representation of the contents of an OVF or OVA."""

    # API methods to be called by clients

    @classmethod
    def detect_type_from_name(cls, filename):
        """Checks the given filename (not file contents, as filename may not
        exist yet) to see whether it appears to describe a file type supported
        by this VM class.
        Returns a string representing the type of this file,
        or raises a ValueUnsupportedError otherwise.
        """
        # We don't care about any directory path
        filename = os.path.basename(filename)
        extension = os.path.splitext(filename)[1]

        if extension == ".ovf" or extension == ".ova":
            return extension
        # Some sources of files are not good about preserving the extension
        # and hence tend to append additional extensions - while this may open
        # us to incorrect behavior (assuming that 'foo.ovf.zip' is a valid OVF
        # when it's probably a zip of an OVF) we'll err on the side of
        # accepting too much rather than incorrectly rejecting something like
        # "foo.ova.2014.05.06A" that's just lazily named.
        m = re.search("(\.ov[fa])[^a-zA-Z0-9]", filename)
        if m:
            extension = m.group(1)
            logger.warning("Filename '{0}' does not end in '.ovf' or '.ova', "
                           "but found '{1}' in mid-filename; treating as such."
                           .format(filename, extension))
            return extension

        raise ValueUnsupportedError("filename", filename, ('.ovf', '.ova'))

    def __init__(self, input_file, output_file):
        """Open the specified OVF and read its XML into memory.
        Note that an output_file of None means we're operating as read-only,
        while an output_file of "" means we plan to write but don't know where.
        """

        super(OVF, self).__init__(input_file, output_file)

        try:
            # Make sure we can write the requested output format, or abort:
            if output_file:
                self.detect_type_from_name(output_file)

            # Make sure we know how to read the input
            extension = self.detect_type_from_name(input_file)
            if extension == '.ova':
                # Untar the ova to our working directory
                self.ovf_descriptor = self.untar(input_file)
            elif extension == '.ovf':
                self.ovf_descriptor = input_file
            else:
                # We should never get here, but be safe...
                raise VMInitError(
                    2,
                    "File {0} does not appear to be an OVA or OVF"
                    .format(input_file))

            # Open the provided OVF
            try:
                self.read_xml(self.ovf_descriptor)
            except ParseError as e:
                raise VMInitError(2, "XML parser error in reading {0}: {1}"
                                  .format(self.ovf_descriptor, str(e)))

            # Quick sanity check before we go any further:
            if ((not re.search("Envelope", self.root.tag)) or
                    (XML.strip_ns(self.root.tag) != 'Envelope')):
                raise VMInitError(
                    2,
                    "File {0} does not appear to be an OVF descriptor - "
                    "expected top-level element {1} but found {2} "
                    "instead!".format(self.ovf_descriptor, 'Envelope',
                                      self.root.tag))

            root_namespace = XML.get_ns(self.root.tag)
            logger.verbose("Root namespace is " + root_namespace)
            if root_namespace == 'http://www.vmware.com/schema/ovf/1/envelope':
                logger.info("OVF version is 0.9")
                self.ovf_version = 0.9
            elif root_namespace == 'http://schemas.dmtf.org/ovf/envelope/1':
                logger.info("OVF version is 1.x")
                self.ovf_version = 1.0
            elif root_namespace == 'http://schemas.dmtf.org/ovf/envelope/2':
                logger.info("OVF version is 2.x")
                self.ovf_version = 2.0
            else:
                raise VMInitError(
                    2,
                    "File {0} has an Envelope but it is in "
                    "unknown namespace {1}"
                    .format(self.ovf_descriptor, root_namespace))
            self.name_helper = OVFNameHelper(self.ovf_version)

            # Some OVF sources are lazy about their XML namespacing, treating
            # the "OVF" namespace as default but not explicitly declaring it
            # as such. So fix things up if necessary:
            try:
                for elem in self.root.iter():
                    # A namespaced element will have its namespace prepended
                    # to the tag like so:
                    # "{http://schemas.dmtf.org/ovf/envelope/1}Envelope"
                    if elem.tag[0] != '{':
                        logger.debug(
                            "Fixing up missing namespace for element {0}"
                            .format(elem.tag))
                        elem.tag = self.OVF + elem.tag
            except AttributeError:
                # 2.6 has getiterator() but not iter()
                for elem in self.root.getiterator():
                    # A namespaced element will have its namespace prepended
                    # to the tag like so:
                    # "{http://schemas.dmtf.org/ovf/envelope/1}Envelope"
                    if elem.tag[0] != '{':
                        logger.debug(
                            "Fixing up missing namespace for element {0}"
                            .format(elem.tag))
                        elem.tag = self.OVF + elem.tag

            for (prefix, URI) in self.NSM.items():
                self.register_namespace(prefix, URI)

            # Register additional non-standard namespaces we're aware of:
            self.register_namespace('vmw', "http://www.vmware.com/schema/ovf")
            self.register_namespace('vbox',
                                    "http://www.virtualbox.org/ovf/machine")

            # Go ahead and set pointers to some of the most useful XML sections
            self.envelope = self.root
            self.references = self.find_child(
                self.envelope,
                self.REFERENCES,
                required=True)
            self.disk_section = self.find_child(
                self.envelope,
                self.DISK_SECTION,
                attrib=self.DISK_SECTION_ATTRIB)
            self.network_section = self.find_child(
                self.envelope,
                self.NETWORK_SECTION,
                attrib=self.NETWORK_SECTION_ATTRIB)
            self.deploy_opt_section = self.find_child(
                self.envelope,
                self.DEPLOY_OPT_SECTION,
                required=False)
            self.virtual_system = self.find_child(
                self.envelope,
                self.VIRTUAL_SYSTEM,
                attrib=self.VIRTUAL_SYSTEM_ATTRIB,
                required=True)
            self.product_section = self.find_child(
                self.virtual_system,
                self.PRODUCT_SECTION)
            self.annotation_section = self.find_child(
                self.virtual_system,
                self.ANNOTATION_SECTION,
                attrib=self.ANNOTATION_SECTION_ATTRIB)
            self.virtual_hw_section = self.find_child(
                self.virtual_system,
                self.VIRTUAL_HW_SECTION,
                attrib=self.VIRTUAL_HW_SECTION_ATTRIB,
                required=True)

            # Initialize various caches
            self.configuration_profiles = []

            try:
                self.hardware = OVFHardware(self)
            except OVFHardwareDataError as e:
                raise VMInitError(1,
                                  "OVF descriptor is invalid: {0}".format(e))

            self.get_platform()

            # Check any file references in the ovf descriptor
            self.validate_file_references()

            if (self.output_file is not None and
                    os.path.dirname(self.ovf_descriptor) != self.working_dir):
                # Copy all referenced files to the working directory
                # Not needed if we're in read-only mode (no output_file)
                input_dir = os.path.dirname(self.ovf_descriptor)
                for file in self.find_all_children(self.references, self.FILE):
                    filepath = os.path.join(input_dir,
                                            file.get(self.FILE_HREF))
                    if not os.path.exists(filepath):
                        logger.warning(
                            "File '{0}' referenced in the OVF does not exist. "
                            "It will not be copied to the working dir '{1}'."
                            .format(file.get(self.FILE_HREF),
                                    self.working_dir))
                        continue
                    logger.debug("Copying {0} to {1}"
                                 .format(filepath, self.working_dir))
                    shutil.copy(filepath, self.working_dir)
        except Exception as e:
            self.destroy()
            raise

    def set_output_file(self, output_file):
        # Make sure we can write the requested output format, or abort:
        if output_file:
            self.detect_type_from_name(output_file)
        self.output_file = output_file

    def __getattr__(self, name):
        """Transparently pass constant name lookups off to our OVFNameHelper.
        """
        return getattr(self.name_helper, name)

    def write(self):
        if not self.output_file:
            return

        prefix = os.path.splitext(self.output_file)[0]
        extension = self.detect_type_from_name(self.output_file)

        # Update the XML ElementTree to reflect any hardware changes
        self.hardware.update_xml()

        # Make sure file references are correct:
        for file in XML.find_all_children(self.references, self.FILE):
            file_path = os.path.join(self.working_dir,
                                     file.get(self.FILE_HREF))
            if not os.path.exists(file_path):
                logger.error(
                    "Referenced file '{0}' does not exist in working "
                    "directory {1} and so will not be included in the output."
                    .format(file.get(self.FILE_HREF), self.working_dir))
                continue
            # FILE_SIZE is optional in the OVF standard
            reported_size = file.get(self.FILE_SIZE)
            file_size_string = str(os.path.getsize(file_path))
            if file_size_string != reported_size:
                if reported_size is not None:
                    logger.warning("Size of file '{0}' seems to have changed "
                                   "from {1} (reported in the original OVF) "
                                   "to {2} (current file size). "
                                   "The updated OVF will reflect this change."
                                   .format(file.get(self.FILE_HREF),
                                           reported_size,
                                           file_size_string))
                file.set(self.FILE_SIZE, file_size_string)

            disk = self.find_disk_from_file_id(file.get(self.FILE_ID))
            if disk is not None:
                capacity = str(self.get_capacity_from_disk(disk))
                actual_capacity = get_disk_capacity(file_path)
                if capacity != actual_capacity:
                    logger.warning(
                        "Capacity of disk '{0}' seems to have changed "
                        "from {1} (reported in the original OVF) "
                        "to {2} (actual capacity). "
                        "The updated OVF will reflect this change."
                        .format(file.get(self.FILE_HREF),
                                capacity, actual_capacity))
                    self.set_capacity_of_disk(disk, actual_capacity)

        logger.info("Writing out to file {0}".format(self.output_file))

        if extension == '.ova':
            ovf_file = os.path.join(self.working_dir, "{0}.ovf"
                                    .format(os.path.basename(prefix)))
            self.write_xml(ovf_file)
            self.generate_manifest(ovf_file)
            self.tar(ovf_file, self.output_file)
        elif extension == '.ovf':
            self.write_xml(self.output_file)
            # Copy all files from working directory to destination
            dest_dir = os.path.dirname(os.path.abspath(self.output_file))
            if not dest_dir:
                dest_dir = os.getcwd()
            for file in self.find_all_children(self.references, self.FILE):
                file_path = os.path.join(self.working_dir,
                                         file.get(self.FILE_HREF))
                if not os.path.exists(file_path):
                    # We already warned about this above
                    continue
                logger.info("Copying {0} to {1}"
                            .format(file.get(self.FILE_HREF),
                                    dest_dir))
                shutil.copy(os.path.join(self.working_dir,
                                         file.get(self.FILE_HREF)),
                            dest_dir)
            # Generate manifest
            self.generate_manifest(self.output_file)
        else:
            # We should never get here, but to be safe:
            raise NotImplementedError("Not sure how to write a '{0}' file"
                                      .format(extension))

    def info_string(self, verbosity_option=None):
        """Returns a string summarizing the contents of this OVF.
        """
        str_list = []
        # File description
        str_list.append('-' * TEXT_WIDTH)
        str_list.append(self.input_file)
        if self.platform and self.platform is not Platform.GenericPlatform:
            str_list.append("COT detected platform type: {0}"
                            .format(self.platform.PLATFORM_NAME))
        str_list.append('-' * TEXT_WIDTH)

        # Product information
        if verbosity_option == 'brief':
            template = "{0:9} {1}"
        else:
            template = "{0:9} {1}\n          {2}"
        p = self.product_section
        if p is not None:
            str_list.append("")
            # All elements in this section are optional
            product = p.find(self.PRODUCT)
            product_url = p.find(self.PRODUCT_URL)
            # TODO: wrap all of these to screen width...
            str_list.append(template.format(
                "Product:",
                (product.text if product is not None
                 else "(No product string)"),
                (product_url.text if product_url is not None
                 else "(No product URL)")))
            vendor = p.find(self.VENDOR)
            vendor_url = p.find(self.VENDOR_URL)
            str_list.append(template.format(
                "Vendor:",
                (vendor.text if vendor is not None
                 else "(No vendor string)"),
                (vendor_url.text if vendor_url is not None
                 else "(No vendor URL)")))
            version = p.find(self.VERSION)
            full_version = p.find(self.FULL_VERSION)
            str_list.append(template.format(
                "Version:",
                (version.text if version is not None
                 else "(No version string)"),
                (full_version.text if full_version is not None
                 else "(No detailed version string)")))

        # Annotation information
        a = self.annotation_section
        if a is not None:
            ann = a.find(self.ANNOTATION)
            if ann is not None and ann.text:
                str_list.append("")
                first = True
                wrapper = textwrap.TextWrapper(
                    width=TEXT_WIDTH,
                    initial_indent='Annotation: ',
                    subsequent_indent='            ')
                for line in ann.text.splitlines():
                    str_list.append(wrapper.fill(line))
                    if first:
                        wrapper.initial_indent = wrapper.subsequent_indent
                        first = False

        # End user license agreement information
        # An OVF may have zero, one, or more
        eula_header = False
        for e in self.find_all_children(self.virtual_system,
                                        self.EULA_SECTION):
            info = e.find(self.INFO)
            lic = e.find(self.EULA_LICENSE)
            if lic is not None and lic.text:
                str_list.append("")
                if not eula_header:
                    str_list.append("End User License Agreement(s):")
                    eula_header = True
                if info is not None and info.text:
                    str_list.append('  ' + info.text)
                if verbosity_option != 'verbose':
                    str_list.append("    (not displayed, use 'cot info "
                                    "--verbose' if desired)")
                else:
                    wrapper = textwrap.TextWrapper(width=TEXT_WIDTH,
                                                   initial_indent='    ',
                                                   subsequent_indent='    ')
                    for line in lic.text.splitlines():
                        str_list.append(wrapper.fill(line))

        # File information
        HREF_W = 36
        SIZE_W = 10
        CAP_W = 10
        DEV_W = 20
        template = ("{{0:{0}}} {{1:>{1}}} {{2:>{2}}} {{3:.{3}}}"
                    .format(HREF_W, SIZE_W, CAP_W, DEV_W))
        file_list = self.references.findall(self.FILE)
        disk_list = (self.disk_section.findall(self.DISK)
                     if self.disk_section is not None else [])
        if file_list or disk_list:
            str_list.append("")
            str_list.append(template.format("Files and Disks:",
                                            "File Size", "Capacity", "Device"))
            str_list.append(template.format("", "----------", "----------",
                                            "--------------------"))
            for file in file_list:
                # FILE_SIZE is optional
                reported_size = file.get(self.FILE_SIZE)
                if reported_size is None:
                    # TODO - check file size in working dir and/or tarfile
                    file_size_str = ""
                else:
                    file_size_str = byte_string(file.get(self.FILE_SIZE))

                disk = self.find_disk_from_file_id(file.get(self.FILE_ID))
                if disk is None:
                    disk_cap_string = ""
                    device_item = self.find_item_from_file(file)
                else:
                    disk_cap_string = byte_string(
                        self.get_capacity_from_disk(disk))
                    device_item = self.find_item_from_disk(disk)
                if device_item is None:
                    device_str = ""
                else:
                    controller_item = self.find_parent_from_item(device_item)
                    device_str = ("{0} @ {1} {2}:{3}".format(
                        self.get_type_from_device(device_item),
                        self.get_type_from_device(controller_item).upper(),
                        controller_item.get_value(self.ADDRESS),
                        device_item.get_value(self.ADDRESS_ON_PARENT)))

                href_str = "  "+file.get(self.FILE_HREF)
                # Truncate to fit in available space
                if len(href_str) > HREF_W:
                    href_str = href_str[:(HREF_W-3)] + "..."
                str_list.append(template.format(href_str,
                                                file_size_str,
                                                disk_cap_string,
                                                device_str))

            # Find placeholder disks as well
            for disk in disk_list:
                file_id = disk.get(self.DISK_FILE_REF)
                file = self.find_child(self.references, self.FILE,
                                       attrib={self.FILE_ID: file_id})
                if file is not None:
                    continue   # already reported on above
                disk_cap_string = byte_string(
                    self.get_capacity_from_disk(disk))
                device_item = self.find_item_from_disk(disk)
                if device_item is None:
                    device_str = ""
                else:
                    controller_item = self.find_parent_from_item(device_item)
                    device_str = ("{0} @ {1} {2}:{3}".format(
                        self.get_type_from_device(device_item),
                        self.get_type_from_device(controller_item).upper(),
                        controller_item.get_value(self.ADDRESS),
                        device_item.get_value(self.ADDRESS_ON_PARENT)))
                str_list.append(template.format("  (disk placeholder)",
                                                "--",
                                                disk_cap_string,
                                                device_str))

        # Supported hardware information
        virtual_system_type = None
        system = self.virtual_hw_section.find(self.SYSTEM)
        if system is not None:
            virtual_system_type = system.find(self.VIRTUAL_SYSTEM_TYPE)
        scsi_subtypes = set()
        for scsi_ctrl in self.hardware.find_all_items('scsi'):
            scsi_subtypes |= scsi_ctrl.get_all_values(self.RESOURCE_SUB_TYPE)
        ide_subtypes = set()
        for ide_ctrl in self.hardware.find_all_items('ide'):
            ide_subtypes |= ide_ctrl.get_all_values(self.RESOURCE_SUB_TYPE)
        eth_subtypes = set()
        for eth in self.hardware.find_all_items('ethernet'):
            eth_subtypes |= eth.get_all_values(self.RESOURCE_SUB_TYPE)

        if ((virtual_system_type is not None) or
                (scsi_subtypes or ide_subtypes or eth_subtypes)):
            str_list.append("")
            str_list.append("Hardware Variants:")
            template = "  {0:25} {1}"
            if virtual_system_type is not None:
                str_list.append(template.format(
                    "System types:", virtual_system_type.text))
            if scsi_subtypes:
                str_list.append(template.format(
                    "SCSI device types:", " ".join(sorted(scsi_subtypes))))
            if ide_subtypes:
                str_list.append(template.format(
                    "IDE device types:", " ".join(sorted(ide_subtypes))))
            if eth_subtypes:
                str_list.append(template.format(
                    "Ethernet device types:", " ".join(sorted(eth_subtypes))))

        # Profile information
        profile_str = self.profile_info_string(verbosity_option)
        if profile_str:
            str_list.append("")
            str_list.append(profile_str)

        # Network information
        if self.network_section is not None:
            str_list.append("")
            str_list.append("Networks:")
            wrapper = textwrap.TextWrapper(width=TEXT_WIDTH,
                                           initial_indent='  ',
                                           subsequent_indent=' ' * 33)
            for network in self.network_section.findall(self.NETWORK):
                network_name = network.get(self.NETWORK_NAME)
                network_desc = network.findtext(self.NWK_DESC, None)
                if verbosity_option == 'verbose' and network_desc:
                    str_list.append(wrapper.fill("{0:30} {1}"
                                                 .format(network_name,
                                                         network_desc)))
                else:
                    str_list.append("  " + network_name)

        # NIC information
        nics = self.hardware.find_all_items('ethernet')
        if nics and verbosity_option != 'brief':
            str_list.append("")
            str_list.append("NICs and Associated Networks:")
            wrapper = textwrap.TextWrapper(width=TEXT_WIDTH,
                                           initial_indent='    ',
                                           subsequent_indent='    ')
            for nic in nics:
                network_name = nic.get_value(self.CONNECTION)
                nic_name = nic.get_value(self.ELEMENT_NAME)
                if nic_name is None:
                    nic_name = "<instance {0}>".format(
                        nic.get_value(self.INSTANCE_ID))
                str_list.append("  {0:30} : {1}"
                                .format(nic_name,
                                        nic.get_value(self.CONNECTION)))
                if verbosity_option == 'verbose':
                    desc = nic.get_value(self.ITEM_DESCRIPTION)
                    if desc is None:
                        desc = nic.get_value(self.CAPTION)
                    if desc is not None:
                        str_list.append(wrapper.fill(desc))

        # Property information
        properties = self.get_property_array()
        if properties:
            str_list.append("")
            str_list.append("Properties:")
            if verbosity_option == 'verbose':
                wrapper = textwrap.TextWrapper(width=TEXT_WIDTH,
                                               initial_indent='      ',
                                               subsequent_indent='      ')
            for ph in properties:
                str_list.append('  {0:30} : {1}'
                                .format(ph['key'], ph['value']))
                if verbosity_option != 'brief':
                    str_list.append('      "{0}"'.format(ph['label']))
                if verbosity_option == 'verbose':
                    for line in ph['description'].splitlines():
                        str_list.append(wrapper.fill(line))

        return "\n".join(str_list)

    def profile_info_string(self, verbosity_option=None, enumerate=False):
        str_list = []
        # Profile information
        PROF_W = 33
        CPU_W = 4
        MEM_W = 9
        NIC_W = 6
        SER_W = 7
        HD_W = 15
        template = (
            "{{0:{0}}} {{1:>{1}}} {{2:>{2}}} {{3:>{3}}} {{4:>{4}}} {{5:>{5}}}"
            .format(PROF_W, CPU_W, MEM_W, NIC_W, SER_W, HD_W))
        str_list.append(template
                        .format("Configuration Profiles:", "CPUs", "Memory",
                                "NICs", "Serials", "Disks/Capacity"))
        str_list.append(template.format("", "----", "---------",
                                        "------", "-------",
                                        "---------------"))
        default_profile_id = self.get_default_profile_name()
        profile_ids = self.get_configuration_profile_ids()
        if not profile_ids:
            profile_ids = [None]
        # Print a table of profiles vs. their hardware
        index = 0
        for profile_id in profile_ids:
            cpus = 0
            cpu_item = self.hardware.find_item('cpu', profile=profile_id)
            if cpu_item:
                cpus = cpu_item.get_value(self.VIRTUAL_QUANTITY,
                                          [profile_id])
            megabytes = 0
            ram_item = self.hardware.find_item('memory', profile=profile_id)
            if ram_item:
                megabytes = ram_item.get_value(self.VIRTUAL_QUANTITY,
                                               [profile_id])
            nics = self.hardware.get_item_count('ethernet', profile_id)
            serials = self.hardware.get_item_count('serial', profile_id)
            disk_count = self.hardware.get_item_count('harddisk',
                                                      profile_id)
            disks_size = 0
            if self.disk_section is not None:
                for disk in self.disk_section.findall(self.DISK):
                    disks_size += self.get_capacity_from_disk(disk)

            if enumerate:
                profile_str = "{0:2}) ".format(index)
            else:
                profile_str = "  "
            profile_str += str(profile_id)
            if profile_id == default_profile_id:
                profile_str += " (default)"
            str_list.append(template.format(
                profile_str,
                cpus,
                byte_string(int(megabytes), base_shift=2),
                nics,
                serials,
                "{0:2} / {1:>9}".format(disk_count,
                                        byte_string(disks_size))))
            if profile_id is not None and verbosity_option != 'brief':
                wrapper = textwrap.TextWrapper(width=TEXT_WIDTH,
                                               initial_indent='    ',
                                               subsequent_indent=(' ' * 21))
                profile = self.find_child(self.deploy_opt_section,
                                          self.CONFIG,
                                          attrib={self.CONFIG_ID: profile_id})
                str_list.append(wrapper.fill(
                    '{0:15} "{1}"'.format("Label:",
                                          profile.findtext(self.CFG_LABEL))))
                str_list.append(wrapper.fill(
                    '{0:15} "{1}"'.format("Description:",
                                          profile.findtext(self.CFG_DESC))))
            index += 1

        return "\n".join(str_list)

    def get_default_profile_name(self):
        default_profile = None
        profiles = self.get_configuration_profile_ids()
        if profiles:
            default_profile = profiles[0]

        return default_profile

    def get_platform(self):
        """Identifies the platform type from the OVF descriptor"""
        if self.platform is not None:
            return self.platform

        platform = None
        product_class = None
        class_to_platform_map = {
            'csr1000v':   Platform.CSR1000V,
            'iosv':       Platform.IOSv,
            'nx-osv':     Platform.NXOSv,
            'ios-xrv':    Platform.IOSXRv,
            'ios-xrv.rp': Platform.IOSXRvRP,
            'ios-xrv.lc': Platform.IOSXRvLC,
            }

        if self.product_section is None:
            platform = Platform.GenericPlatform
        else:
            product_class = self.product_section.get(self.PRODUCT_CLASS)
            if product_class is None:
                platform = Platform.GenericPlatform
            else:
                match = re.match("com\.cisco\.(.*)", product_class)
                if match:
                    class_type = match.group(1)
                    try:
                        platform = class_to_platform_map[class_type]
                    except KeyError:
                        pass
        if not platform:
            logger.warning("Unrecognized product class '{0}' - known classes "
                           "are {1}. Treating as generic product..."
                           .format(product_class,
                                   class_to_platform_map.keys()))
            platform = Platform.GenericPlatform
        logger.info("OVF product class {0} --> platform {1}"
                    .format(product_class, platform.__name__))
        self.platform = platform
        return self.platform

    def validate_file_references(self):
        """Check whether all files in the References exist and are correctly
        described.
        Returns True (all files valid) or False (one or more missing/invalid).
        """
        if not self.output_file:
            if self.input_file != self.ovf_descriptor:
                # Read-only mode, handling an OVA input file.
                # In this case we only untarred the ovf descriptor
                # and not any other contents of the OVA, so there's
                # nothing to check here.
                return True
        base_dir = os.path.dirname(self.ovf_descriptor)
        rc = True
        for file in XML.find_all_children(self.references, self.FILE):
            # Does the file exist?
            file_name = file.get(self.FILE_HREF)
            file_path = os.path.join(base_dir, file_name)
            if not os.path.exists(file_path):
                logger.warning("OVF descriptor references file '{0}', "
                               "but file does not exist!".format(file_name))
                rc = False
                continue

            # Is the file size correct?
            expected_size = file.get(self.FILE_SIZE)
            actual_size = str(os.path.getsize(file_path))
            if expected_size is not None and expected_size != actual_size:
                logger.warning(
                    "OVF descriptor describes file '{0}' as having "
                    "size {1} bytes, but file is actually {2} bytes."
                    .format(file_name, expected_size, actual_size))
                rc = False
                continue

            # Does the file checksum match the manifest, if any? TODO

        return rc

    def get_configuration_profile_ids(self):
        """Get the list of supported configuration profile identifiers.
        If this OVF has no defined profiles, returns an empty list.
        If there is a default profile, it will be first in the list.
        """
        if ((not self.configuration_profiles) and
                (self.deploy_opt_section is not None)):
            profile_ids = []
            profiles = self.find_all_children(self.deploy_opt_section,
                                              self.CONFIG)
            for profile in profiles:
                # Force the "default" profile to the head of the list
                if (profile.get(self.CONFIG_DEFAULT) == 'true' or
                        profile.get(self.CONFIG_DEFAULT) == '1'):
                    profile_ids.insert(0, profile.get(self.CONFIG_ID))
                else:
                    profile_ids.append(profile.get(self.CONFIG_ID))
            logger.verbose("Configuration profiles are: {0}"
                           .format(profile_ids))
            self.configuration_profiles = profile_ids
        return self.configuration_profiles

    def create_configuration_profile(self, id, label, description):
        """Create or update a configuration profile with the given ID"""
        self.deploy_opt_section = self.create_envelope_section_if_absent(
            self.DEPLOY_OPT_SECTION, "Configuration Profiles")

        cfg = self.find_child(self.deploy_opt_section, self.CONFIG,
                              attrib={self.CONFIG_ID: id})
        if cfg is None:
            logger.debug("Creating new Configuration element")
            cfg = ET.SubElement(self.deploy_opt_section, self.CONFIG,
                                {self.CONFIG_ID: id})

        self.set_or_make_child(cfg, self.CFG_LABEL, label)
        self.set_or_make_child(cfg, self.CFG_DESC, description)
        # Clear cache
        self.configuration_profiles = []

    def set_system_type(self, type_list):
        """Set the virtual system type(s) supported by this virtual machine.
        For an OVF, this corresponds to the VirtualSystemType element.
        """
        type_string = " ".join(type_list)
        logger.info("Setting VirtualSystemType to '{0}'".format(type_string))
        system = self.virtual_hw_section.find(self.SYSTEM)
        if system is None:
            system = XML.set_or_make_child(self.virtual_hw_section,
                                           self.SYSTEM,
                                           ordering=(self.INFO, self.SYSTEM,
                                                     self.ITEM))
            # A System must have some additional children to be valid:
            XML.set_or_make_child(system, self.VSSD + "ElementName",
                                  "Virtual System Type")
            XML.set_or_make_child(system, self.VSSD + "InstanceID", 0)
        XML.set_or_make_child(system, self.VIRTUAL_SYSTEM_TYPE, type_string)

    def set_cpu_count(self, cpus, profile_list):
        """Set the number of CPUs"""
        logger.info("Updating CPU count in OVF under profile {0} to {1}"
                    .format(profile_list, cpus))
        self.platform.validate_cpu_count(cpus)
        self.hardware.set_value_for_all_items('cpu',
                                              self.VIRTUAL_QUANTITY, cpus,
                                              profile_list,
                                              create_new=True)

    def set_memory(self, megabytes, profile_list):
        """Set the amount of RAM, in megabytes"""
        logger.info("Updating RAM in OVF under profile {0} to {1}"
                    .format(profile_list, megabytes))
        self.platform.validate_memory_amount(megabytes)
        self.hardware.set_value_for_all_items('memory',
                                              self.VIRTUAL_QUANTITY, megabytes,
                                              profile_list,
                                              create_new=True)

    def set_nic_type(self, type, profile_list):
        """Set the hardware type for NICs"""
        self.platform.validate_nic_type(type)
        self.hardware.set_value_for_all_items('ethernet',
                                              self.RESOURCE_SUB_TYPE,
                                              type.upper(),
                                              profile_list)

    def get_nic_count(self, profile_list):
        """Get the number of NICs under the given profile(s).
        Returns a dictionary of profile_name:nic_count.
        """
        return self.hardware.get_item_count_per_profile('ethernet',
                                                        profile_list)

    def set_nic_count(self, count, profile_list):
        """Set the given profile(s) to have the given number of NICs.
        """
        logger.info("Updating NIC count in OVF under profile {0} to {1}"
                    .format(profile_list, count))
        self.platform.validate_nic_count(count)
        self.hardware.set_item_count_per_profile('ethernet', count,
                                                 profile_list)

    def get_network_list(self):
        """Gets the list of network names currently defined in this VM.
        """
        if self.network_section is None:
            return []
        return [network.get(self.NETWORK_NAME) for
                network in self.network_section.findall(self.NETWORK)]

    def create_network(self, label, description):
        """Define a new network with the given label and description.
        """
        self.network_section = self.create_envelope_section_if_absent(
            self.NETWORK_SECTION,
            "Logical networks",
            attrib=self.NETWORK_SECTION_ATTRIB)
        network = self.set_or_make_child(self.network_section, self.NETWORK,
                                         attrib={self.NETWORK_NAME: label})
        self.set_or_make_child(network, self.NWK_DESC, description)

    def set_nic_networks(self, network_list, profile_list):
        """Set the NIC to network mapping for NICs under the given profile(s).
        """
        # If len(network_list) is less than the number of NICs,
        # set all remaining NICs to the last item in network_list
        self.hardware.set_item_values_per_profile('ethernet',
                                                  self.CONNECTION,
                                                  network_list,
                                                  profile_list,
                                                  default=network_list[-1])

    def set_nic_mac_addresses(self, mac_list, profile_list):
        """Set the MAC addresses for NICs under the given profile(s).
        """
        # If len(mac_list) is less than the number of NICs,
        # set all remaining NICs to the last item in mac_list
        self.hardware.set_item_values_per_profile('ethernet',
                                                  self.ADDRESS,
                                                  mac_list,
                                                  profile_list,
                                                  default=mac_list[-1])

    def set_nic_names(self, name_list, profile_list):
        """Set the device names for NICs under the given profile(s).
        """
        # Expand the pattern (if any) out to a full list
        nic_dict = self.get_nic_count(profile_list)
        max_nics = max(nic_dict.values())
        if len(name_list) < max_nics:
            logger.info("Expanding name_list {0} to {1} entries"
                        .format(name_list, max_nics))
            # Extract the pattern and remove it from the list
            pattern = name_list[-1]
            name_list = name_list[:-1]
            # Look for the magic string {0}, {1}, etc in the pattern
            match = re.search("{(\d+)}", pattern)
            if match:
                i = int(match.group(1))
            else:
                i = 0
            while len(name_list) < max_nics:
                name_list.append(re.sub("{\d+}", str(i), pattern))
                i += 1
            logger.info("New name_list is {0}".format(name_list))

        self.hardware.set_item_values_per_profile('ethernet',
                                                  self.ELEMENT_NAME,
                                                  name_list,
                                                  profile_list)

    def get_serial_count(self, profile_list):
        """Get the number of serial ports under the given profile(s).
        Returns a dictionary of profile_name:serial_count.
        """
        return self.hardware.get_item_count_per_profile('serial', profile_list)

    def set_serial_count(self, count, profile_list):
        """Set the given profile(s) to have the given number of serial ports.
        """
        logger.info("Updating serial port count under profile {0} to {1}"
                    .format(profile_list, count))
        self.hardware.set_item_count_per_profile('serial', count, profile_list)

    def set_serial_connectivity(self, conn_list, profile_list):
        """Set the serial port connectivity under the given profile(s).
        """
        self.hardware.set_item_values_per_profile('serial',
                                                  self.ADDRESS, conn_list,
                                                  profile_list, default="")

    def set_scsi_subtype(self, type, profile_list):
        """Set the SCSI controller(s) subtype"""
        # TODO validate supported types by platform
        self.hardware.set_value_for_all_items('scsi',
                                              self.RESOURCE_SUB_TYPE, type,
                                              profile_list)

    def set_ide_subtype(self, type, profile_list):
        """Set the IDE controller(s) subtype"""
        # TODO validate supported types by platform
        self.hardware.set_value_for_all_items('ide',
                                              self.RESOURCE_SUB_TYPE, type,
                                              profile_list)

    def set_short_version(self, version_string):
        """Set the Version element's value"""
        if self.product_section is None:
            self.product_section = self.set_or_make_child(
                self.virtual_system, self.PRODUCT_SECTION)
            # Any Section must have an Info as child
            self.set_or_make_child(self.product_section, self.INFO,
                                   "Product Information")
        logger.info("Updating Version element in OVF")
        self.set_or_make_child(self.product_section, self.VERSION,
                               version_string)

    def set_long_version(self, version_string):
        """Set the FullVersion element's value"""
        if self.product_section is None:
            self.product_section = self.set_or_make_child(
                self.virtual_system, self.PRODUCT_SECTION)
            # Any Section must have an Info as child
            self.set_or_make_child(self.product_section, self.INFO,
                                   "Product Information")
        logger.info("Updating FullVersion element in OVF")
        self.set_or_make_child(self.product_section, self.FULL_VERSION,
                               version_string)

    def get_property_array(self):
        """Get an array of property hashes, each with the keys
        {key, value, qualifiers, type, label, description}"""
        result = []
        if self.product_section is None:
            return result
        elems = self.find_all_children(self.product_section, self.PROPERTY)
        for elem in elems:
            label = elem.findtext(self.PROPERTY_LABEL, "")
            descr = elem.findtext(self.PROPERTY_DESC, "")
            result.append({
                'key': elem.get(self.PROP_KEY),
                'value': elem.get(self.PROP_VALUE),
                'qualifiers': elem.get(self.PROP_QUAL, ""),
                'type': elem.get(self.PROP_TYPE, ""),
                'label': label,
                'description': descr,
            })

        return result

    def get_property_value(self, key):
        """Get the value of the given property, or None
        """
        if self.product_section is None:
            return None
        property = self.find_child(self.product_section, self.PROPERTY,
                                   attrib={self.PROP_KEY: key})
        if property is None:
            return None
        return property.get(self.PROP_VALUE)

    def set_property_value(self, key, value):
        """Set the value of the given property (converting value if needed).
        Returns the value that was set.
        """
        if self.product_section is None:
            self.product_section = self.set_or_make_child(
                self.virtual_system, self.PRODUCT_SECTION)
            # Any Section must have an Info as child
            self.set_or_make_child(self.product_section, self.INFO,
                                   "Product Information")
        property = self.find_child(self.product_section, self.PROPERTY,
                                   attrib={self.PROP_KEY: key})
        if property is None:
            self.set_or_make_child(self.product_section, self.PROPERTY,
                                   attrib={self.PROP_KEY: key,
                                           self.PROP_VALUE: value,
                                           self.PROP_TYPE: 'string'})
            return value

        # Else, make sure the requested value is valid
        prop_type = property.get(self.PROP_TYPE, "")
        if prop_type == "boolean":
            # OVF contains a string representation of a boolean
            value = str(bool(value)).lower()
        elif prop_type == "string":
            value = str(value)

        prop_qual = property.get(self.PROP_QUAL, "")
        if prop_qual:
            m = re.search("MaxLen\((\d+)\)", prop_qual)
            if m:
                max_len = int(m.group(1))
                if len(value) > max_len:
                    raise ValueUnsupportedError(
                        key, value, "string no longer than {0} characters"
                        .format(max_len))
            m = re.search("MinLen\((\d+)\)", prop_qual)
            if m:
                min_len = int(m.group(1))
                if len(value) < min_len:
                    raise ValueUnsupportedError(
                        key, value, "string no shorter than {0} characters"
                        .format(min_len))

        property.set(self.PROP_VALUE, value)
        return value

    def config_file_to_properties(self, file):
        """Import each line of the provided file into a configuration property
        """
        i = 0
        if not self.platform.LITERAL_CLI_STRING:
            raise NotImplementedError("no known support for literal CLI on " +
                                      self.platform.PLATFORM_NAME)
        with open(file, 'r') as f:
            for line in f:
                line = line.strip()
                # Skip blank lines and comment lines
                if (not line) or line[0] == '!':
                    continue
                i += 1
                self.set_property_value(
                    "{0}-{1:04d}".format(self.platform.LITERAL_CLI_STRING, i),
                    line)

    def convert_disk_if_needed(self, file_path, kind):
        """Convert all hard disk files to stream-optimized VMDK (the only
        format that ESXi supports in OVA packages); CD-ROM iso
        images are left alone.
        """
        if kind != 'harddisk':
            logger.debug("No disk conversion needed")
            return file_path

        # Convert hard disk to VMDK format, streamOptimized subformat
        return convert_disk_image(file_path, self.working_dir,
                                  'vmdk', 'streamOptimized')

    def search_from_filename(self, filename):
        """Returns the tuple (file, disk, ctrl_item, disk_item),
        any or all of which may be None, based on using the given filename
        to find a matching File in the OVF, then using that to find a matching
        Disk and Items"""
        file = None
        disk = None
        ctrl_item = None
        disk_item = None

        logger.verbose("Looking for existing disk info based on filename {0}"
                       .format(filename))

        file = self.find_child(self.references, self.FILE,
                               attrib={self.FILE_HREF: filename})

        if file is None:
            return (file, disk, ctrl_item, disk_item)

        file_id = file.get(self.FILE_ID)
        disk = self.find_disk_from_file_id(file_id)

        disk_item_1 = self.find_item_from_file(file)
        disk_item_2 = self.find_item_from_disk(disk)
        disk_item = check_for_conflict("disk Item", [disk_item_1, disk_item_2])

        ctrl_item = self.find_parent_from_item(disk_item)

        if disk_item is not None and ctrl_item is None:
            raise LookupError("Found disk Item {0} but no controller Item "
                              "as its parent?"
                              .format(disk_item))

        return (file, disk, ctrl_item, disk_item)

    def search_from_file_id(self, file_id):
        """Returns the tuple (file, disk, ctrl_item, disk_item),
        any or all of which may be None, based on using the given file_id
        to find a matching File in the OVF, then using that to find a matching
        Disk and Items"""
        if file_id is None:
            return (None, None, None, None)

        logger.verbose(
            "Looking for existing disk information based on file_id {0}"
            .format(file_id))

        file = None
        disk = None
        ctrl_item = None
        disk_item = None

        file = self.find_child(self.references, self.FILE,
                               attrib={self.FILE_ID: file_id})

        disk = self.find_disk_from_file_id(file_id)

        if disk is not None and file is None:
            # Should never happen - OVF is not valid
            raise LookupError("Malformed OVF? Found Disk with fileRef {0} but "
                              "no corresponding File with id {0}"
                              .format(file_id))

        disk_item_1 = self.find_item_from_file(file)
        disk_item_2 = self.find_item_from_disk(disk)
        disk_item = check_for_conflict("disk Item", [disk_item_1, disk_item_2])

        ctrl_item = self.find_parent_from_item(disk_item)

        if disk_item is not None and ctrl_item is None:
            raise LookupError("Found disk Item {0} but no controller Item "
                              "as its parent?"
                              .format(disk_item))

        return (file, disk, ctrl_item, disk_item)

    def search_from_controller(self, controller, address):
        """Returns the tuple (file, disk, ctrl_item, disk_item),
        any or all of which may be None, based on using the given controller
        type and disk address to find controller and disk Items, then using
        the disk Item to find File and/or Disk"""

        if controller is None or address is None:
            return (None, None, None, None)

        logger.verbose("Looking for existing disk information based on "
                       "controller type ({0}) and disk address ({1})"
                       .format(controller, address))

        file = None
        disk = None
        ctrl_item = None
        disk_item = None

        ctrl_addr = address.split(":")[0]
        disk_addr = address.split(":")[1]
        logger.debug("Searching for controller address {0}"
                     .format(ctrl_addr))

        ctrl_item = self.hardware.find_item(controller,
                                            {self.ADDRESS: ctrl_addr})

        if ctrl_item is None:
            return (file, disk, ctrl_item, disk_item)

        # From controller Item to its child disk Item
        ctrl_instance = ctrl_item.get_value(self.INSTANCE_ID)
        logger.debug("Searching for disk address {0} with parent {1}"
                     .format(disk_addr, ctrl_instance))
        disk_item = self.hardware.find_item(
            properties={self.PARENT: ctrl_instance,
                        self.ADDRESS_ON_PARENT: disk_addr})

        if disk_item is None:
            return (file, disk, ctrl_item, disk_item)

        host_resource = disk_item.get_value(self.HOST_RESOURCE)
        if host_resource is None:
            logger.debug("Disk item has no RASD:HostResource - "
                         "i.e., empty drive")
            return (file, disk, ctrl_item, disk_item)

        if (host_resource.startswith(self.HOST_RSRC_DISK_REF) or
                host_resource.startswith(self.OLD_HOST_RSRC_DISK_REF)):
            logger.debug("Looking for Disk and File matching disk Item")
            # From disk Item to Disk
            disk_id = os.path.basename(host_resource)
            if self.disk_section is not None:
                disk = self.find_child(self.disk_section, self.DISK,
                                       attrib={self.DISK_ID: disk_id})

            if disk is not None:
                # From Disk to File
                file_id = disk.get(self.DISK_FILE_REF)
                file = self.find_child(self.references, self.FILE,
                                       attrib={self.FILE_ID: file_id})
        elif (host_resource.startswith(self.HOST_RSRC_FILE_REF) or
              host_resource.startswith(self.OLD_HOST_RSRC_FILE_REF)):
            logger.debug("Looking for File and Disk matching disk Item")
            # From disk Item to File
            file_id = os.path.basename(host_resource)
            file = self.find_child(self.references, self.FILE,
                                   attrib={self.FILE_ID: file_id})

            if self.disk_section is not None:
                disk = self.find_child(self.disk_section, self.DISK,
                                       attrib={self.DISK_FILE_REF: file_id})
        else:
            raise LookupError("Unrecognized HostResource format '{0}'"
                              .format(host_resource))

        return (file, disk, ctrl_item, disk_item)

    def find_open_controller(self, type):
        """Find the first controller Item of the requested type that has an
        open slot, and return the Item, and the full address of the open slot
        """

        for ctrl_item in self.hardware.find_all_items(type):
            ctrl_instance = ctrl_item.get_value(self.INSTANCE_ID)
            ctrl_addr = ctrl_item.get_value(self.ADDRESS)
            logger.debug("Found controller instance {0} address {1}"
                         .format(ctrl_instance, ctrl_addr))
            disk_list = self.hardware.find_all_items(
                properties={self.PARENT: ctrl_instance})
            address_list = [disk.get_value(self.ADDRESS_ON_PARENT) for
                            disk in disk_list]
            disk_addr = 0
            while str(disk_addr) in address_list:
                disk_addr += 1
            if ((type == 'scsi' and disk_addr > 7) or
                    (type == 'ide' and disk_addr > 1)):
                logger.info("Controller address {0} is already full"
                            .format(ctrl_addr))
            else:
                logger.info("Found open slot {0}:{1}".format(ctrl_addr,
                                                             disk_addr))
                return (ctrl_item, "{0}:{1}".format(ctrl_addr, disk_addr))

        logger.info("No open controller found")
        return (None, None)

    def get_id_from_file(self, file):
        """Return the ID value of the given file reference"""
        return file.get(self.FILE_ID)

    def get_path_from_file(self, file):
        """Return the filename/path of the given file reference"""
        return file.get(self.FILE_HREF)

    def get_file_ref_from_disk(self, disk):
        """Return the file reference value of the given disk"""
        return disk.get(self.DISK_FILE_REF)

    def get_type_from_device(self, device):
        """Return the type string for the given device"""
        type = device.get_value(self.RESOURCE_TYPE)
        for key in self.RES_MAP.keys():
            if type == self.RES_MAP[key]:
                return key
        raise ValueUnsupportedError(self.RESOURCE_TYPE,
                                    type,
                                    self.RES_MAP.values())

    def get_subtype_from_device(self, device):
        """Return the subtype string for the given device"""
        return device.get_value(self.RESOURCE_SUB_TYPE)

    def get_common_subtype(self, type):
        """Get the sub-type common to all devices of the given type. If
        multiple such devices exist and they do not all have the same sub-type,
        returns None."""
        subtype = None
        for item in self.hardware.find_all_items(type):
            item_subtype = item.get_value(self.RESOURCE_SUB_TYPE)
            if subtype is None:
                subtype = item_subtype
                logger.info("Found {0} subtype {1}".format(type, subtype))
            elif subtype != item_subtype:
                logger.warning("Found conflicting subtypes ('{0}', '{1}') for "
                               "device type {2}".format(subtype, item_subtype,
                                                        type))
                return None
        return subtype

    def check_sanity_of_disk_device(self, disk, file, disk_item, ctrl_item):
        """Make sure the indicated disk device has appropriate linkage to any
        disk, file, and controller provided. Die if it does not."""
        if disk_item is None:
            return
        if ctrl_item is not None:
            match_or_die("disk Item Parent", disk_item.get_value(self.PARENT),
                         "controller Item InstanceID",
                         ctrl_item.get_value(self.INSTANCE_ID))
        host_resource = disk_item.get_value(self.HOST_RESOURCE)
        if host_resource is not None:
            if ((host_resource.startswith(self.HOST_RSRC_DISK_REF) or
                 host_resource.startswith(self.OLD_HOST_RSRC_DISK_REF))
                    and disk is not None):
                match_or_die("disk Item HostResource",
                             os.path.basename(host_resource),
                             "Disk diskId", disk.get(self.DISK_ID))
            if ((host_resource.startswith(self.HOST_RSRC_FILE_REF) or
                 host_resource.startswith(self.OLD_HOST_RSRC_FILE_REF))
                    and file is not None):
                match_or_die("disk Item HostResource",
                             os.path.basename(host_resource),
                             "File id", file.get(self.FILE_ID))

    def add_file(self, file_path, file_id, file=None, disk=None):
        """Add the given file path and ID to the VM, overwriting the
        provided file object (if any)"""

        logger.debug("Adding File to OVF")

        if file is not None:
            file.clear()
        elif disk is None:
            file = ET.SubElement(self.references, self.FILE)
        else:
            # The OVF standard requires that Disks which reference a File
            # be listed in the same order as the Files.
            # Since there's already a Disk, make sure the new File is ordered
            # appropriately.
            # This is complicated by the fact that we may have
            # Files which are not Disks and Disks with no backing File.
            all_files = self.references.findall(self.FILE)
            all_disks = self.disk_section.findall(self.DISK)

            # Starting from the Disk entry corresponding to our new File,
            # search forward until we find the next Disk (if any) which has a
            # File, and insert our new File before that File.
            disk_index = all_disks.index(disk)
            file_index = len(all_files)
            while disk_index < len(all_disks):
                tmp_file_id = all_disks[disk_index].get(self.DISK_FILE_REF)
                next_file = self.find_child(self.references, self.FILE,
                                            attrib={self.FILE_ID: tmp_file_id})
                if next_file is not None:
                    file_index = all_files.index(next_file)
                    break
                disk_index += 1

            file = ET.Element(self.FILE)
            self.references.insert(file_index, file)

        file_size_string = str(os.path.getsize(file_path))
        file_name = os.path.basename(file_path)

        file.set(self.FILE_ID, file_id)
        file.set(self.FILE_HREF, file_name)
        file.set(self.FILE_SIZE, file_size_string)

        # Copy the file to our working directory if it's not already there
        if os.path.dirname(file_path) != self.working_dir:
            shutil.copy(file_path, os.path.join(self.working_dir, file_name))

        return file

    def add_disk(self, file_path, file_id, disk_type, disk=None):
        """Add a disk referencing the given file to the VM, overwriting the
        provided disk object (if any)"""
        if disk_type != 'harddisk':
            logger.debug("Not adding Disk element to OVF, as CD-ROMs do not "
                         "require a Disk")
            return disk

        self.disk_section = self.create_envelope_section_if_absent(
            self.DISK_SECTION,
            "Virtual disk information",
            attrib=self.DISK_SECTION_ATTRIB)

        logger.debug("Adding Disk to OVF")

        if disk is not None:
            disk_id = disk.get(self.DISK_ID)
            disk.clear()
        else:
            disk_id = file_id
            disk = ET.SubElement(self.disk_section, self.DISK)

        capacity = get_disk_capacity(file_path)
        self.set_capacity_of_disk(disk, capacity)

        disk.set(self.DISK_ID, disk_id)
        disk.set(self.DISK_FILE_REF, file_id)
        disk.set(self.DISK_FORMAT,
                 ("http://www.vmware.com/interfaces/"
                  "specifications/vmdk.html#streamOptimized"))
        return disk

    def add_controller_device(self, type, subtype, address, ctrl_item=None):
        """Create a new IDE or SCSI controller, or update existing one"""
        if ctrl_item is None:
            logger.info("Controller not found, adding new Item")
            (ctrl_instance, ctrl_item) = self.hardware.new_item(type)
            if address is None:
                # Find a controller address that isn't already used
                address_list = [
                    ci.get_value(self.ADDRESS) for
                    ci in self.hardware.find_all_items(type)]
                address = 0
                while str(address) in address_list:
                    address += 1
                logger.info("Selected address {0} for new controller"
                            .format(address))
            if type == "scsi" and int(address) > 3:
                raise ValueTooHighError("SCSI controller address", address, 3)
            elif type == "ide" and int(address) > 1:
                raise ValueTooHighError("IDE controller address", address, 1)
            ctrl_item.set_property(self.ADDRESS, address)
            ctrl_item.set_property(self.ELEMENT_NAME, "{0} Controller"
                                   .format(type.upper()))
            ctrl_item.set_property(self.ITEM_DESCRIPTION,
                                   "{0} Controller {1}"
                                   .format(type.upper(), address))
        # Change subtype of existing controller or new controller
        if subtype is not None:
            ctrl_item.set_property(self.RESOURCE_SUB_TYPE, subtype)
        return ctrl_item

    def add_disk_device(self, type, address, name, description, disk, file,
                        ctrl_item, disk_item=None):
        """Create a new disk hardware device or overwrite existing one"""
        if disk_item is None:
            logger.info("Disk Item not found, adding new Item")
            ctrl_instance = ctrl_item.get_value(self.INSTANCE_ID)
            disk_instance = self.hardware.find_unused_instance_id()
            if address is None:
                logger.debug("Working to identify address of new disk")

                items = self.hardware.find_all_items(
                    properties={self.PARENT: ctrl_instance})
                addresses = [item.get_value(self.ADDRESS_ON_PARENT) for
                             item in items]
                address = 0
                while str(address) in addresses:
                    address += 1
                logger.warning("New disk address on parent not specified, "
                               "guessing it should be {0}".format(address))
            ctrl_type = self.get_type_from_device(ctrl_item)
            # Make sure the address is valid!
            if ctrl_type == "scsi" and int(address) > 15:
                raise ValueTooHighError("disk address on SCSI controller",
                                        address, 15)
            elif ctrl_type == "ide" and int(address) > 1:
                raise ValueTooHighError("disk address on IDE controller",
                                        address, 1)

            if name is None:
                if type == 'cdrom':
                    name = "CD-ROM Drive"
                elif type == 'harddisk':
                    name = "Hard Disk Drive"
                else:
                    # Should never get here!
                    raise ValueUnsupportedError("disk type", type,
                                                "'cdrom' or 'harddisk'")

            (disk_instance, disk_item) = self.hardware.new_item(type)
            disk_item.set_property(self.ADDRESS_ON_PARENT, address)
            disk_item.set_property(self.PARENT, ctrl_instance)
        else:
            logger.debug("Updating existing disk Item")

        # Make these changes to the disk Item regardless of new/existing
        if type == 'harddisk':
            # Link to the Disk we created
            disk_item.set_property(self.HOST_RESOURCE,
                                   (self.HOST_RSRC_DISK_REF +
                                    disk.get(self.DISK_ID)))
        else:
            # No Disk for CD-ROM; link to the File instead
            disk_item.set_property(self.HOST_RESOURCE,
                                   (self.HOST_RSRC_FILE_REF +
                                    file.get(self.FILE_ID)))

        if name is not None:
            disk_item.set_property(self.ELEMENT_NAME, name)
        if description is not None:
            disk_item.set_property(self.ITEM_DESCRIPTION, description)

        return disk_item

    # Helper methods - for internal use only

    def untar(self, file):
        """Untar the provided .ova to a temporary location and return the
        resulting .ovf file path"""

        logger.verbose("Untarring {0} to working directory {1}"
                       .format(file, self.working_dir))

        try:
            tarf = tarfile.open(file, 'r')
        except (EOFError, tarfile.TarError) as e:
            raise VMInitError(1, "Could not untar {0}: {1}"
                              .format(file, e.args))

        try:
            # The OVF standard says, with regard to OVAs:
            # ...the files shall be in the following order inside the archive:
            # 1) OVF descriptor
            # 2) OVF manifest (optional)
            # 3) OVF certificate (optional)
            # 4) The remaining files shall be in the same order as listed
            #    in the References section...
            # 5) OVF manifest (optional)
            # 6) OVF certificate (optional)
            #
            # For now we just validate #1.
            if not tarf.getmembers():
                raise VMInitError(1, "No files to untar from {0}!"
                                  .format(file))
            ovf_descriptor = tarf.getmembers()[0]
            if os.path.splitext(ovf_descriptor.name)[1] != '.ovf':
                raise VMInitError(1,
                                  "First file in {0} is '{1}' but it should "
                                  "have been an OVF file - OVA is invalid!"
                                  .format(file, ovf_descriptor.name))
            # Make sure the provided file doesn't contain any malicious paths
            # http://stackoverflow.com/questions/8112742/
            for n in tarf.getnames():
                logger.debug("Examining path of {0} prior to untar".format(n))
                if not (os.path.abspath(os.path.join(self.working_dir, n))
                        .startswith(self.working_dir)):
                    raise VMInitError(1, "Tar file contains malicious/unsafe "
                                      "file path '{0}'!".format(n))

            if self.output_file is not None:
                # Extract all of the contents
                tarf.extractall(path=self.working_dir)
                logger.verbose("Extracted all files in {0} to working dir {1}"
                               .format(file, self.working_dir))
            else:
                # Read-only mode - only extract the OVF descriptor file.
                tarf.extract(ovf_descriptor, path=self.working_dir)
                logger.verbose(
                    "Extracted OVF descriptor from {0} to working dir {1}"
                    .format(file, self.working_dir))
        finally:
            tarf.close()

        # Find the OVF file
        return os.path.join(self.working_dir, ovf_descriptor.name)

    def generate_manifest(self, ovf_file):
        """Construct the manifest file for this package, if possible.
        Returns True if the manifest was successfully generated,
        False if not successful (such as if checksum helper tools are
        unavailable).
        """
        (prefix, extension) = os.path.splitext(ovf_file)
        logger.verbose("Generating manifest for {0}".format(ovf_file))
        manifest = prefix + '.mf'
        # TODO: OVF 2.0 uses SHA256 instead of SHA1.
        sha1sum = get_checksum(ovf_file, 'sha1')
        with open(manifest, 'wb') as f:
            f.write("SHA1({file})= {sum}\n"
                    .format(file=os.path.basename(ovf_file), sum=sha1sum)
                    .encode('utf-8'))
            # Checksum all referenced files as well
            for file in self.find_all_children(self.references, self.FILE):
                file_name = file.get(self.FILE_HREF)
                file_path = os.path.join(os.path.dirname(ovf_file), file_name)
                if not os.path.exists(file_path):
                    logger.error("File {0} does not exist - unable to "
                                 "determine its checksum for the manifest."
                                 .format(file_name))
                    continue
                sha1sum = get_checksum(file_path, 'sha1')
                f.write("SHA1({file})= {sum}\n"
                        .format(file=file_name, sum=sha1sum)
                        .encode('utf-8'))
        logger.debug("Manifest generated successfully")
        return True

    def tar(self, ovf_descriptor, tar_file):
        """Tar the provided .ovf to generate the requested .ova"""

        logger.verbose("Creating tar file {0}".format(tar_file))

        dir = os.path.dirname(ovf_descriptor)
        (prefix, extension) = os.path.splitext(ovf_descriptor)

        tarf = tarfile.open(tar_file, 'w')

        try:
            # OVF is always first
            logger.verbose("Adding {0} to {1}".format(ovf_descriptor,
                                                      tar_file))
            tarf.add(ovf_descriptor, os.path.basename(ovf_descriptor))
            # Add manifest if present
            manifest_path = prefix + '.mf'
            if os.path.exists(manifest_path):
                logger.verbose("Adding manifest to {0}".format(tar_file))
                tarf.add(manifest_path, os.path.basename(manifest_path))
            if os.path.exists("{0}.cert".format(prefix)):
                logger.warning("Don't know how to re-sign a certificate file, "
                               "so the existing certificate will be omitted "
                               "from {0}.".format(tar_file))
            # Add all other files mentioned in the OVF
            for file in self.find_all_children(self.references, self.FILE):
                file_path = os.path.join(dir, file.get(self.FILE_HREF))
                if not os.path.exists(file_path):
                    logger.error("As referenced file '{0}' does not exist, "
                                 "it will not be added to the OVA"
                                 .format(file.get(self.FILE_HREF)))
                    continue
                tarf.add(file_path, os.path.basename(file_path))
                logger.verbose("Added {0} to {1}".format(file_path, tar_file))
        finally:
            tarf.close()

    def create_envelope_section_if_absent(self, section_tag, info_string,
                                          attrib={}):
        """If the OVF doesn't already have the given type of Section,
        create it and set its info string. Returns the Section found/created"""
        section = self.find_child(self.envelope, section_tag, attrib=attrib)
        if section is not None:
            return section

        logger.info("No existing {0}. Creating it."
                    .format(XML.strip_ns(section_tag)))
        section = ET.Element(section_tag, attrib=attrib)
        # Section elements may be in arbitrary order relative to one another,
        # but they MUST come after the References and before the VirtualSystem.
        # We'll construct them immediately before the VirtualSystem.
        i = 0
        for child in list(self.envelope):
            if child.tag == self.VIRTUAL_SYSTEM:
                break
            i += 1
        self.envelope.insert(i, section)

        # All Sections must have an Info child
        self.set_or_make_child(section, self.INFO, info_string)

        return section

    def find_parent_from_item(self, item):
        """Finds the parent Item of the given Item and returns it, or None."""
        if item is None:
            return None

        parent_instance = item.get_value(self.PARENT)
        if parent_instance is None:
            logger.error("Provided Item has no RASD:Parent element?")
            return None

        return self.hardware.find_item(
            properties={self.INSTANCE_ID: parent_instance})

    def find_item_from_disk(self, disk):
        """Returns the disk Item (or None) that references the given Disk"""
        if disk is None:
            return None

        disk_id = disk.get(self.DISK_ID)

        match = self.hardware.find_item(
            properties={
                self.HOST_RESOURCE: (self.HOST_RSRC_DISK_REF + disk_id)
            })
        if not match:
            match = self.hardware.find_item(
                properties={
                    self.HOST_RESOURCE: (self.OLD_HOST_RSRC_DISK_REF + disk_id)
                })
        return match

    def find_item_from_file(self, file):
        """Returns the disk Item (or None) that references the given File"""
        if file is None:
            return None

        file_id = file.get(self.FILE_ID)
        match = self.hardware.find_item(
            properties={
                self.HOST_RESOURCE: (self.HOST_RSRC_FILE_REF + file_id)
            })
        if not match:
            match = self.hardware.find_item(
                properties={
                    self.HOST_RESOURCE: (self.OLD_HOST_RSRC_FILE_REF + file_id)
                })
        return match

    def find_disk_from_file_id(self, file_id):
        """Uses the given file_id to find a matching Disk in the OVF"""
        if file_id is None or self.disk_section is None:
            return None

        return self.find_child(self.disk_section, self.DISK,
                               attrib={self.DISK_FILE_REF: file_id})

    def find_empty_drive(self, type):
        """Returns a disk device (if any) of the requested type
        that exists but contains no data.
        """
        if type == 'cdrom':
            # Find a drive that has no HostResource property
            return self.hardware.find_item(
                resource_type=type,
                properties={self.HOST_RESOURCE: None})
        elif type == 'harddisk':
            # All harddisk items must have a HostResource, so we need a
            # different way to indicate an empty drive. By convention,
            # we do this with a small placeholder disk (one with a Disk entry
            # but no corresponding File included in the OVF package).
            if self.disk_section is None:
                logger.verbose("No DiskSection, so no placeholder disk!")
                return None
            for disk in self.disk_section.findall(self.DISK):
                file_id = disk.get(self.DISK_FILE_REF)
                if file_id is None:
                    # Found placeholder disk!
                    # Now find the drive that's using this disk.
                    return self.find_item_from_disk(disk)
            logger.verbose("No placeholder disk found.")
            return None
        else:
            raise ValueUnsupportedError("drive type",
                                        type,
                                        "'cdrom' or 'harddisk'")

    def find_device_location(self, device):
        """Returns the tuple (type, address), such as ("ide", "1:0")
        associated with the given device.
        """
        controller = self.find_parent_from_item(device)
        if controller is None:
            raise LookupError("No parent controller for device?")
        return (self.get_type_from_device(controller),
                (controller.get_value(self.ADDRESS) + ':' +
                 device.get_value(self.ADDRESS_ON_PARENT)))

    def get_capacity_from_disk(self, disk):
        """Returns the capacity of the given Disk in bytes
        """
        cap = int(disk.get(self.DISK_CAPACITY))
        cap_units = disk.get(self.DISK_CAP_UNITS, 'byte')
        return byte_count(cap, cap_units)

    def set_capacity_of_disk(self, disk, capacity_bytes):
        """Sets the capacity of the given Disk in the most human-readable
        form (i.e., 8 GB instead of 8589934592 bytes)
        """
        if self.ovf_version < 1.0:
            # In OVF 0.9 only bytes is supported as a unit
            disk.set(self.DISK_CAPACITY, capacity_bytes)
        else:
            (capacity, cap_units) = factor_bytes(capacity_bytes)
            disk.set(self.DISK_CAPACITY, capacity)
            disk.set(self.DISK_CAP_UNITS, cap_units)


class OVFNameHelper(object):
    """Helper class for OVF.
    Provides string constants for easier lookup of various OVF XML
    elements and attributes.
    """

    def __init__(self, version):
        self.ovf_version = version
        self.platform = None
        # For the standard namespace URIs in an OVF descriptor, let's define
        # shorthand identifiers to be used when writing back out to XML:
        cim_uri = "http://schemas.dmtf.org/wbem/wscim/1"
        self.NSM = {
            'xsi':  "http://www.w3.org/2001/XMLSchema-instance",
            'cim':  cim_uri + "/common",
            'rasd': (cim_uri +
                     "/cim-schema/2/CIM_ResourceAllocationSettingData"),
            'vssd': cim_uri + "/cim-schema/2/CIM_VirtualSystemSettingData",
        }
        # Non-standard namespaces (such as VMWare's
        # 'http://www.vmware.com/schema/ovf') should not be added to the NSM
        # dictionary, but may be registered manually by calling
        # self.register_namespace() as needed - see self.write() for examples.

        if self.ovf_version < 1.0:
            self.NSM['ovf'] = "http://www.vmware.com/schema/ovf/1/envelope"
        elif self.ovf_version < 2.0:
            self.NSM['ovf'] = "http://schemas.dmtf.org/ovf/envelope/1"
        else:
            self.NSM['ovf'] = "http://schemas.dmtf.org/ovf/envelope/2"

        if self.ovf_version >= 2.0:
            self.NSM['epasd'] = "http://schemas.dmtf.org/wbem/wscim/1/\
cim-schema/2/CIM_EthernetPortAllocationSettingData.xsd"
            self.NSM['sasd'] = "http://schemas.dmtf.org/wbem/wscim/1/\
cim-schema/2/CIM_StorageAllocationSettingData.xsd"

        # Shortcuts for finding/creating elements in various namespaces
        self.OVF = ('{' + self.NSM['ovf'] + '}')
        self.RASD = ('{' + self.NSM['rasd'] + '}')
        self.VSSD = ('{' + self.NSM['vssd'] + '}')
        self.XSI = ('{' + self.NSM['xsi'] + '}')
        if self.ovf_version >= 2.0:
            self.EPASD = ('{' + self.NSM['epasd'] + '}')
            self.SASD = ('{' + self.NSM['sasd'] + '}')
        else:
            # Older OVF versions have ethernet and storage items
            # in the same RASD namespace as other hardware
            self.EPASD = self.RASD
            self.SASD = self.RASD

        OVF = self.OVF
        VSSD = self.VSSD
        XSI = self.XSI

        # XML elements we care about in the OVF descriptor
        # Top-level element is Envelope
        self.ENVELOPE = OVF + 'Envelope'

        # All Section elements have an Info element as child
        self.INFO = OVF + 'Info'

        # Envelope -> NetworkSection -> Network
        if self.ovf_version < 1.0:
            self.NETWORK_SECTION = OVF + 'Section'
            self.NETWORK_SECTION_ATTRIB = {
                XSI + 'type': "ovf:NetworkSection_Type"
            }
        else:
            self.NETWORK_SECTION = OVF + 'NetworkSection'
            self.NETWORK_SECTION_ATTRIB = {}
        self.NETWORK = OVF + 'Network'
        # Attributes of a Network element
        self.NETWORK_NAME = OVF + 'name'
        # Network sub-elements
        self.NWK_DESC = OVF + 'Description'

        # Envelope -> DeploymentOptionSection -> Configuration
        self.DEPLOY_OPT_SECTION = OVF + 'DeploymentOptionSection'
        self.CONFIG = OVF + 'Configuration'
        # Attributes of a Configuration element
        self.CONFIG_ID = OVF + 'id'
        self.CONFIG_DEFAULT = OVF + 'default'
        # Configuration sub-elements
        self.CFG_LABEL = OVF + 'Label'
        self.CFG_DESC = OVF + 'Description'

        # Envelope -> References -> File
        self.REFERENCES = OVF + 'References'
        self.FILE = OVF + 'File'
        # Attributes of a File element
        self.FILE_ID = OVF + 'id'
        self.FILE_HREF = OVF + 'href'
        self.FILE_SIZE = OVF + 'size'

        # Envelope -> DiskSection -> Disk
        if self.ovf_version < 1.0:
            self.DISK_SECTION = OVF + 'Section'
            self.DISK_SECTION_ATTRIB = {
                XSI + 'type': "ovf:DiskSection_Type"
            }
        else:
            self.DISK_SECTION = OVF + 'DiskSection'
            self.DISK_SECTION_ATTRIB = {}
        self.DISK = OVF + 'Disk'
        # Attributes of a Disk element
        self.DISK_ID = OVF + 'diskId'
        self.DISK_FILE_REF = OVF + 'fileRef'
        self.DISK_CAPACITY = OVF + 'capacity'
        self.DISK_CAP_UNITS = OVF + 'capacityAllocationUnits'
        self.DISK_FORMAT = OVF + 'format'

        # Envelope -> VirtualSystem -> AnnotationSection -> Annotation
        if self.ovf_version < 1.0:
            self.ANNOTATION_SECTION = OVF + 'Section'
            self.ANNOTATION_SECTION_ATTRIB = {
                XSI + 'type': "ovf:AnnotationSection_Type"
            }
        else:
            self.ANNOTATION_SECTION = OVF + 'AnnotationSection'
            self.ANNOTATION_SECTION_ATTRIB = {}
        self.ANNOTATION = OVF + 'Annotation'

        # Envelope -> VirtualSystem -> ProductSection
        if self.ovf_version < 1.0:
            self.VIRTUAL_SYSTEM = OVF + 'Content'
            self.VIRTUAL_SYSTEM_ATTRIB = {
                XSI + 'type': "ovf:VirtualSystem_Type"
            }
        else:
            self.VIRTUAL_SYSTEM = OVF + 'VirtualSystem'
            self.VIRTUAL_SYSTEM_ATTRIB = {}
        self.PRODUCT_SECTION = OVF + 'ProductSection'
        # ProductSection attributes
        self.PRODUCT_CLASS = OVF + 'class'
        # ProductSection sub-elements
        self.PRODUCT = OVF + 'Product'
        self.VENDOR = OVF + 'Vendor'
        self.VERSION = OVF + 'Version'
        self.FULL_VERSION = OVF + 'FullVersion'
        self.PRODUCT_URL = OVF + 'ProductUrl'
        self.VENDOR_URL = OVF + 'VendorUrl'
        self.PROPERTY = OVF + 'Property'
        # Attributes of a Property element
        self.PROP_KEY = OVF + 'key'
        self.PROP_VALUE = OVF + 'value'
        self.PROP_QUAL = OVF + 'qualifiers'
        self.PROP_TYPE = OVF + 'type'
        # Property sub-elements
        self.PROPERTY_LABEL = OVF + 'Label'
        self.PROPERTY_DESC = OVF + 'Description'

        # Envelope -> VirtualSystem -> EulaSection -> License
        self.EULA_SECTION = OVF + 'EulaSection'
        self.EULA_LICENSE = OVF + 'License'

        # Envelope -> VirtualSystem -> VirtualHardwareSection -> Item(s)
        # In version 2.x, there can also be StorageItem and EthernetPortItem
        if self.ovf_version < 1.0:
            self.VIRTUAL_HW_SECTION = OVF + 'Section'
            self.VIRTUAL_HW_SECTION_ATTRIB = {
                XSI + 'type': "ovf:VirtualHardwareSection_Type"
            }
        else:
            self.VIRTUAL_HW_SECTION = OVF + 'VirtualHardwareSection'
            self.VIRTUAL_HW_SECTION_ATTRIB = {}
        self.ITEM = OVF + 'Item'
        if self.ovf_version >= 2.0:
            self.STORAGE_ITEM = OVF + 'StorageItem'
            self.ETHERNET_PORT_ITEM = OVF + 'EthernetPortItem'
        else:
            # These are just regular Items in older OVF versions
            self.STORAGE_ITEM = self.ITEM
            self.ETHERNET_PORT_ITEM = self.ITEM
        # Item attributes
        self.ITEM_CONFIG = OVF + 'configuration'
        # Item sub-elements
        # As these are shared across the RASD, SASD, and EPASD namespaces
        # in OVF 2.0, we don't hard-code a namespace any more.
        self.ADDRESS = 'Address'
        self.ADDRESS_ON_PARENT = 'AddressOnParent'
        self.ALLOCATION_UNITS = 'AllocationUnits'
        self.AUTOMATIC_ALLOCATION = 'AutomaticAllocation'
        self.AUTOMATIC_DEALLOCATION = 'AutomaticDeallocation'
        if self.ovf_version < 1.0:
            self.BUS_NUMBER = 'BusNumber'
        self.CAPTION = 'Caption'
        self.CONNECTION = 'Connection'
        self.CONSUMER_VISIBILITY = 'ConsumerVisibility'
        self.ITEM_DESCRIPTION = 'Description'
        if self.ovf_version < 1.0:
            # No ElementName in 0.9, but Caption serves a similar purpose
            self.ELEMENT_NAME = self.CAPTION
        else:
            self.ELEMENT_NAME = 'ElementName'
        self.HOST_RESOURCE = 'HostResource'
        self.OLD_HOST_RSRC_FILE_REF = "/file/"
        self.OLD_HOST_RSRC_DISK_REF = "/disk/"
        if self.ovf_version < 1.0:
            self.HOST_RSRC_FILE_REF = "/file/"
            self.HOST_RSRC_DISK_REF = "/disk/"
            self.INSTANCE_ID = 'InstanceId'
        else:
            self.HOST_RSRC_FILE_REF = "ovf:/file/"
            self.HOST_RSRC_DISK_REF = "ovf:/disk/"
            self.INSTANCE_ID = 'InstanceID'
        self.LIMIT = 'Limit'
        self.MAPPING_BEHAVIOR = 'MappingBehavior'
        self.OTHER_RESOURCE_TYPE = 'OtherResourceType'
        self.PARENT = 'Parent'
        self.POOL_ID = 'PoolID'
        self.RESERVATION = 'Reservation'
        self.RESOURCE_SUB_TYPE = 'ResourceSubType'
        self.RESOURCE_TYPE = 'ResourceType'
        self.VIRTUAL_QUANTITY = 'VirtualQuantity'
        self.WEIGHT = 'Weight'

        # Children of Item must be in a specific order, which differs from
        # 0.9 versus 1.0+ OVF versions:
        if self.ovf_version < 1.0:
            self.ITEM_CHILDREN = (
                self.CAPTION,
                self.ITEM_DESCRIPTION,
                self.INSTANCE_ID,
                self.RESOURCE_TYPE,
                self.OTHER_RESOURCE_TYPE,
                self.RESOURCE_SUB_TYPE,
                self.POOL_ID,
                self.CONSUMER_VISIBILITY,
                self.HOST_RESOURCE,
                self.ALLOCATION_UNITS,
                self.VIRTUAL_QUANTITY,
                self.RESERVATION,
                self.LIMIT,
                self.WEIGHT,
                self.AUTOMATIC_ALLOCATION,
                self.AUTOMATIC_DEALLOCATION,
                self.PARENT,
                self.CONNECTION,
                self.ADDRESS,
                self.MAPPING_BEHAVIOR,
                self.ADDRESS_ON_PARENT,
                self.BUS_NUMBER,
            )
        else:
            # 1.0 is nice in that they're all in alphabetical order
            self.ITEM_CHILDREN = (
                self.ADDRESS,
                self.ADDRESS_ON_PARENT,
                self.ALLOCATION_UNITS,
                self.AUTOMATIC_ALLOCATION,
                self.AUTOMATIC_DEALLOCATION,
                self.CAPTION,
                self.CONNECTION,
                self.CONSUMER_VISIBILITY,
                self.ITEM_DESCRIPTION,
                self.ELEMENT_NAME,
                self.HOST_RESOURCE,
                self.INSTANCE_ID,
                self.LIMIT,
                self.MAPPING_BEHAVIOR,
                self.OTHER_RESOURCE_TYPE,
                self.PARENT,
                self.POOL_ID,
                self.RESERVATION,
                self.RESOURCE_SUB_TYPE,
                self.RESOURCE_TYPE,
                self.VIRTUAL_QUANTITY,
                self.WEIGHT,
            )

        # ... VirtualHardwareSection -> System -> VirtualSystemType
        self.SYSTEM = OVF + 'System'
        self.VIRTUAL_SYSTEM_TYPE = VSSD + 'VirtualSystemType'

        # List of ResourceType string values we know about
        self.RES_MAP = {
            'cpu':       '3',
            'memory':    '4',
            'ide':       '5',
            'scsi':      '6',
            'fc':        '7',
            'iscsi':     '8',
            'ib':        '9',
            'ethernet': '10',
            'floppy':   '14',
            'cdrom':    '15',
            'dvd':      '16',
            'harddisk': '17',
            'sata':     '20',   # 'Other Storage' but VBox uses for SATA
            'serial':   '21',
            'parallel': '22',
            'usb':      '23',
        }


class OVFHardwareDataError(Exception):
    """Error raised if the input data used to construct an OVFHardware object
    is not sane."""


class OVFHardware:
    """Helper class for OVF. Represents all hardware items defined by this OVF;
    i.e., the contents of all Items in the VirtualHardwareSection.

    Fundamentally it's just a dict of OVFItem objects with a bunch of helper
    methods.
    """

    def __init__(self, ovf):
        """Construct an OVFHardware object describing the hardware Items
        contained in this OVF object.
        May raise an OVFHardwareDataError if any data errors are seen.
        """
        self.ovf = ovf
        self.item_dict = {}
        valid_profiles = set(ovf.get_configuration_profile_ids())
        item_count = 0
        for item in ovf.virtual_hw_section:
            if item.tag == self.ovf.ITEM:
                NS = self.ovf.RASD
            elif item.tag == self.ovf.STORAGE_ITEM:
                NS = self.ovf.SASD
            elif item.tag == self.ovf.ETHERNET_PORT_ITEM:
                NS = self.ovf.EPASD
            else:
                continue
            item_count += 1
            # We index the dict by InstanceID as it's the one property of
            # an Item that uniquely identifies this set of hardware items.
            instance = item.find(NS + self.ovf.INSTANCE_ID).text

            # Pre-sanity check - are all of the profiles associated with this
            # item properly defined in the OVF DeploymentOptionSection?
            item_profiles = set(item.get(self.ovf.ITEM_CONFIG, "").split())
            unknown_profiles = item_profiles - valid_profiles
            if unknown_profiles:
                raise OVFHardwareDataError("Unknown profile(s) {0} for "
                                           "Item instance {1}"
                                           .format(unknown_profiles, instance))

            if instance not in self.item_dict:
                self.item_dict[instance] = OVFItem(self.ovf, item)
            else:
                try:
                    self.item_dict[instance].add_item(item)
                except OVFItemDataError as e:
                    logger.debug(e)
                    # Mask away the nitty-gritty details from our caller
                    raise OVFHardwareDataError("Data conflict for instance {0}"
                                               .format(instance))
        logger.verbose(
            "OVF contains {0} hardware Item elements describing {1} "
            "unique devices"
            .format(item_count, len(self.item_dict)))
        # Treat the current state as golden:
        for ovfitem in self.item_dict.values():
            ovfitem.modified = False

    def update_xml(self):
        """If any changes have been made to the hardware definitions,
        regenerate any Items in the OVF VirtualHardwareSection to reflect this.
        """
        modified = False
        for ovfitem in self.item_dict.values():
            if ovfitem.modified:
                modified = True
                break
        if not modified:
            logger.debug("No changes to hardware definition, "
                         "so no XML update is required")
            return
        # Delete the existing Items:
        delete_count = 0
        for item in list(self.ovf.virtual_hw_section):
            if (item.tag == self.ovf.ITEM or
                    item.tag == self.ovf.STORAGE_ITEM or
                    item.tag == self.ovf.ETHERNET_PORT_ITEM):
                self.ovf.virtual_hw_section.remove(item)
                delete_count += 1
        logger.verbose("Cleared {0} existing items from VirtualHWSection"
                       .format(delete_count))
        # Generate the new XML Items, in appropriately sorted order by Instance
        ordering = [self.ovf.INFO, self.ovf.SYSTEM, self.ovf.ITEM]
        for instance in natural_sort(self.item_dict.keys()):
            logger.debug("Writing Item(s) with InstanceID {0}"
                         .format(instance))
            ovfitem = self.item_dict[instance]
            new_items = ovfitem.generate_items()
            logger.debug("Generated {0} items".format(len(new_items)))
            for item in new_items:
                XML.add_child(self.ovf.virtual_hw_section, item, ordering)
        logger.verbose("Updated XML VirtualHardwareSection, now contains {0} "
                       "Items representing {1} devices"
                       .format(
                           len(self.ovf.virtual_hw_section.findall(
                               self.ovf.ITEM)),
                           len(self.item_dict)))

    def find_unused_instance_id(self):
        """Find the first unused InstanceID number and return it as a string.
        """
        i = 1
        while str(i) in self.item_dict.keys():
            i += 1
        logger.debug("Found unused InstanceID {0}".format(i))
        return str(i)

    def new_item(self, resource_type, profile_list=None):
        """Create a new OVFItem of the given type with a new InstanceID
        under the given profile(s).
        Returns (instance, ovfitem).
        """
        instance = self.find_unused_instance_id()
        ovfitem = OVFItem(self.ovf)
        ovfitem.set_property(self.ovf.INSTANCE_ID, instance, profile_list)
        ovfitem.set_property(self.ovf.RESOURCE_TYPE,
                             self.ovf.RES_MAP[resource_type],
                             profile_list)
        # ovftool freaks out if we leave out the ElementName on an Item,
        # so provide a simple default value.
        ovfitem.set_property(self.ovf.ELEMENT_NAME, resource_type,
                             profile_list)
        self.item_dict[instance] = ovfitem
        ovfitem.modified = True
        logger.info("Added new {0} under {1}, instance is {2}"
                    .format(resource_type, profile_list, instance))
        return (instance, ovfitem)

    def clone_item(self, parent_item, profile_list):
        """Clone the given OVFItem to create a new instance of this Item under
        the given profile(s).
        Returns (instance, ovfitem).
        """
        instance = self.find_unused_instance_id()
        ovfitem = OVFItem(self.ovf)
        for profile in profile_list:
            ovfitem.add_profile(profile, from_item=parent_item)
        ovfitem.set_property(self.ovf.INSTANCE_ID, instance, profile_list)
        ovfitem.modified = True
        self.item_dict[instance] = ovfitem
        logger.debug("Added clone of {0} under {1}, instance is {2}"
                     .format(parent_item, profile_list, instance))
        return (instance, ovfitem)

    def find_all_items(self, resource_type=None, properties=None,
                       profile_list=None):
        """Find all OVFItems of the given resource_type and other properties,
        and return a list, sorted by InstanceID.
        """
        items = [self.item_dict[instance] for instance in
                 natural_sort(self.item_dict.keys())]
        filtered_items = []
        if properties is None:
            properties = {}
        for ovfitem in items:
            if resource_type and (self.ovf.RES_MAP[resource_type] !=
                                  ovfitem.get_value(self.ovf.RESOURCE_TYPE)):
                continue
            if profile_list and not [ovfitem.has_profile(profile) for
                                     profile in profile_list]:
                continue
            valid = True
            for (property, value) in properties.items():
                if ovfitem.get_value(property) != value:
                    valid = False
                    break
            if valid:
                filtered_items.append(ovfitem)
        logger.debug("Found {0} {1} Items"
                     .format(len(filtered_items), resource_type))
        return filtered_items

    def find_item(self, resource_type=None, properties=None, profile=None):
        """Find the only OVFItem of the given resource_type, or None.
        Will raise a LookupError if more than one such item exists.
        """
        matches = self.find_all_items(resource_type, properties, profile)
        if len(matches) > 1:
            raise LookupError("Found multiple matching {0} Items:\n{2}"
                              .format(resource_type, "\n".join(matches)))
        elif len(matches) == 0:
            return None
        else:
            return matches[0]

    def get_item_count(self, resource_type, profile):
        return (self.get_item_count_per_profile(resource_type, [profile])
                [profile])

    def get_item_count_per_profile(self, resource_type, profile_list):
        """Returns the number of Items of the given resource type per profile,
        as a dict. Items present under "no profile" will be counted against
        the total for each profile.
        """
        count_dict = {}
        if not profile_list:
            # Get the count under all profiles
            profile_list = self.ovf.get_configuration_profile_ids() + [None]
        for profile in profile_list:
            count_dict[profile] = 0
        for ovfitem in self.find_all_items(resource_type):
            for profile in profile_list:
                if ovfitem.has_profile(profile):
                    count_dict[profile] += 1
        for (profile, count) in count_dict.items():
            logger.debug("Profile '{0}' has {1} {2} Item(s)"
                         .format(profile, count, resource_type))
        return count_dict

    def set_item_count_per_profile(self, resource_type, count, profile_list):
        """Sets the number of items of the given type under the given
        profile(s).

        If the new count is greater than the current count under this
        profile, then additional instances that already exist under
        another profile will be added to this profile, starting with
        the lowest-sequence instance not already present, and only as
        a last resort will new instances be created.

        If the new count is less than the current count under this profile,
        then the highest-numbered instances will be removed preferentially.
        """
        if not profile_list:
            # Set the profile list for all profiles, including the default
            profile_list = self.ovf.get_configuration_profile_ids() + [None]
        count_dict = self.get_item_count_per_profile(resource_type,
                                                     profile_list)
        items_seen = dict.fromkeys(profile_list, 0)
        last_item = None

        # First, iterate over existing Items.
        # Once we've seen "count" items under a profile, remove all subsequent
        # items from this profile.
        # If we don't have enough items under a profile, add any items found
        # under other profiles to this profile as well.
        for ovfitem in self.find_all_items(resource_type):
            last_item = ovfitem
            for profile in profile_list:
                if ovfitem.has_profile(profile):
                    if items_seen[profile] >= count:
                        # Too many items - remove this one!
                        ovfitem.remove_profile(profile)
                    else:
                        items_seen[profile] += 1
                else:
                    if count_dict[profile] < count:
                        # Add this profile to this Item
                        ovfitem.add_profile(profile)
                        count_dict[profile] += 1
                        items_seen[profile] += 1

        # How many new Items do we need to create in total?
        items_to_add = 0
        for profile in profile_list:
            delta = count - items_seen[profile]
            if delta > items_to_add:
                items_to_add = delta

        logger.debug("Creating {0} new items".format(items_to_add))
        while items_to_add > 0:
            # Which profiles does this Item need to belong to?
            new_item_profiles = []
            for profile in profile_list:
                if count_dict[profile] < count:
                    new_item_profiles.append(profile)
                    count_dict[profile] += 1
                    items_seen[profile] += 1
            if last_item is None:
                logger.warning("No existing items found for {0}. "
                               "Creating new {0}"
                               .format(resource_type))
                (new_instance, new_item) = self.new_item(resource_type,
                                                         new_item_profiles)
            else:
                (new_instance, new_item) = self.clone_item(last_item,
                                                           new_item_profiles)
            # Check/update other properties of the clone that should be unique:
            address = new_item.get(self.ovf.ADDRESS)
            if address:
                raise NotImplementedError("Don't know how to ensure a unique "
                                          "Address value when cloning an Item "
                                          "of type {0}".format(resource_type))

            address_on_parent = new_item.get(self.ovf.ADDRESS_ON_PARENT)
            if address_on_parent:
                address_set = new_item.get_all_values(
                    self.ovf.ADDRESS_ON_PARENT)
                if len(address_set) > 1:
                    raise NotImplementedError("AddressOnParent is not common "
                                              "across all profiles but has "
                                              "multiple values {0}. COT can't "
                                              "handle this yet."
                                              .format(address_set))
                address_on_parent = address_set.pop()
                # Currently we only handle integer addresses
                try:
                    address_on_parent = int(address_on_parent)
                    address_on_parent += 1
                    new_item.set_property(self.ovf.ADDRESS_ON_PARENT,
                                          str(address_on_parent),
                                          new_item_profiles)
                except ValueError:
                    raise NotImplementedError("Don't know how to ensure a "
                                              "unique AddressOnParent value "
                                              "given base value '{0}'"
                                              .format(address_on_parent))

            if resource_type == 'ethernet':
                # Update ElementName to reflect the NIC number
                # TODO - we assume that the count is the same across profiles
                element_name = self.ovf.platform.guess_nic_name(
                    count_dict[new_item_profiles[0]])
                new_item.set_property(self.ovf.ELEMENT_NAME, element_name,
                                      new_item_profiles)
            last_item = new_item
            items_to_add -= 1

    def set_value_for_all_items(self, resource_type, property, new_value,
                                profile_list, create_new=False):
        """For all items of the given resource_type, update the given property
        to the given new_value under the specified set of profiles.
        If no items of the given type exist, will create a new Item if
        create_new is set to True; otherwise will log a warning and do nothing.
        """
        ovfitem_list = self.find_all_items(resource_type)
        if not ovfitem_list:
            if not create_new:
                logger.warning("No items of type {0} found. Nothing to do."
                               .format(resource_type))
                return
            logger.warning("No existing items of type {0} found. Will create "
                           "new {0} from scratch.".format(resource_type))
            (instance, ovfitem) = self.new_item(resource_type, profile_list)
            ovfitem_list = [ovfitem]
        for ovfitem in ovfitem_list:
            ovfitem.set_property(property, new_value, profile_list)
        logger.info("Updated {0} {1} to {2} under {3}"
                    .format(resource_type, property, new_value,
                            profile_list))

    def set_item_values_per_profile(self, resource_type, property, value_list,
                                    profile_list, default=None):
        """Set the given property for the list of items of the given
        resource type under the given profile(s) to the list of values
        provided.
        """
        if profile_list is None:
            profile_list = self.ovf.get_configuration_profile_ids() + [None]
        for ovfitem in self.find_all_items(resource_type):
            if len(value_list):
                new_value = value_list.pop(0)
            else:
                new_value = default
            for profile in profile_list:
                if ovfitem.has_profile(profile):
                    ovfitem.set_property(property, new_value, [profile])
            logger.info("Updated {0} property {1} to {2} under {3}"
                        .format(resource_type, property,
                                new_value, profile_list))
        if len(value_list):
            logger.error("After scanning all known {0} Items, not all "
                         "{1} values were used - leftover {2}"
                         .format(resource_type, property,
                                 value_list))


class OVFItemDataError(Exception):
    """Error raised when data to be added to an OVFItem conflicts with
    existing data in this OVFItem."""


class OVFItem:
    """Helper class for OVF. Represents all variations of a given hardware
    Item amongst different hardware configuration profiles.
    In essence, it is:
    a dict of Item properties (indexed by element name)
    each of which is a dict of sets of profiles (indexed by element value)
    """

    # Magic strings
    ATTRIB_KEY_SUFFIX = " {Item attribute}"
    ELEMENT_KEY_SUFFIX = " {custom element}"

    def __init__(self, ovf, item=None):
        """Create a new OVFItem with contents based on the given Item element
        """
        self.ovf = ovf
        if ovf is not None:
            self.name_helper = ovf
        else:
            self.name_helper = OVFNameHelper(1.0)
        self.property_dict = {}
        self.modified = False
        self.NS = self.RASD   # default for most item types
        if item is not None:
            self.add_item(item)

    def __repr__(self):
        return ("<OVFItem: {0}>".format(self.property_dict.items()))

    def __str__(self):
        str = "OVFItem:\n"
        for (key, value_dict) in self.property_dict.items():
            str += "  " + key + "\n"
            for (value, profile_set) in value_dict.items():
                str += "    {0:20} : {1}\n".format(value, profile_set)
        return str

    def __getattr__(self, name):
        # Pass through to designated helper
        return getattr(self.name_helper, name)

    def add_item(self, item):
        """Add the given Item element to this OVFItem.
        Will raise an OVFItemDataError if the new Item conflicts with
        existing data in the OVFItem.
        """
        logger.debug("Adding new {0}".format(item.tag))
        if item.tag == self.ITEM:
            self.NS = self.RASD
        elif item.tag == self.STORAGE_ITEM:
            self.NS = self.SASD
        elif item.tag == self.ETHERNET_PORT_ITEM:
            self.NS = self.EPASD
        else:
            raise ValueUnsupportedError("item",
                                        item.tag,
                                        "Item, StorageItem, EthernetPortItem")

        profiles = set(item.get(self.ITEM_CONFIG, "").split())
        # Store any attributes of the Item itself:
        for (attrib, value) in item.attrib.items():
            if attrib == self.ITEM_CONFIG:
                continue
            attrib_string = attrib + self.ATTRIB_KEY_SUFFIX
            self.set_property(attrib_string, value, profiles, overwrite=False)

        # Store any child elements of the Item.
        # We need to iterate in reverse order because we want to be able
        # to reference the VirtualQuantity and ResourceSubType
        # when inspecting the ElementName and Description elements.
        for child in reversed(list(item)):
            if XML.strip_ns(child.tag) not in self.ITEM_CHILDREN:
                # Non-standard elements may not follow the standard rules -
                # for example, VMware OVF extensions may have multiple
                # vmw:Config elements, each distinguished by its vmw:key attr.
                # Rather than try to guess how these items do or do not match,
                # we simply store the whole item
                self.set_property((ET.tostring(child).decode().strip() +
                                   self.ELEMENT_KEY_SUFFIX),
                                  ET.tostring(child).decode(),
                                  profiles, overwrite=False)
                continue
            # Store the value of this element:
            tag = XML.strip_ns(child.tag)
            self.set_property(tag, child.text, profiles, overwrite=False)
            # Store any attributes of this element
            for (attrib, value) in child.attrib.items():
                attrib_string = tag + "_attrib_" + attrib
                self.set_property(attrib_string, value, profiles,
                                  overwrite=False)

        self.modified = True
        logger.debug("Added {0} - new status:\n{1}".format(item.tag,
                                                           str(self)))
        self.validate()

    def set_property(self, key, value, profiles=None, overwrite=True):
        """Store the profiles associated with the given value for
        the given key.
        If profiles is None, replace the value for all profiles currently
        known to this item.
        If overwrite is set to False, trying to set a value that is already
        set for the provided profile will raise a OVFItemDataError.
        (Otherwise, by default, the existing data will be overwritten).
        """
        # Just to be safe...
        value = str(value)

        if key == self.RESOURCE_TYPE:
            if value == self.RES_MAP['ethernet']:
                self.NS = self.EPASD
            elif (value == self.RES_MAP['harddisk'] or
                  value == self.RES_MAP['cdrom']):
                self.NS = self.SASD
            else:
                self.NS = self.RASD

        if not profiles:
            value_dict = self.property_dict.get(key, {})
            if not value_dict:
                # No previous values for this property,
                # and no specified profile set to use.
                # So mark this property as applicable to all profiles.
                profiles = set([None])
            else:
                # Mark this property as applicable to all profiles currently
                # used by this property.
                profiles = set.union(*value_dict.values())
        profiles = set(profiles)
        # If the ElementName or Description references the VirtualQuantity
        # or ResourceSubType, replace that reference with a placeholder
        # that we can regenerate at output time. That way, if the
        # VirtualQuantity or ResourceSubType changes, these can change too.
        if key == self.ELEMENT_NAME or key == self.ITEM_DESCRIPTION:
            vq_val = self.get_value(self.VIRTUAL_QUANTITY, profiles)
            if vq_val is not None:
                value = re.sub(vq_val, "_VQ_", value)
            rst_val = self.get_value(self.RESOURCE_SUB_TYPE, profiles)
            if rst_val is not None:
                value = re.sub(rst_val, "_RST_", value)
        # Similarly, if the Description references the ElementName...
        if key == self.ITEM_DESCRIPTION:
            en_val = self.get_value(self.ELEMENT_NAME, profiles)
            if en_val is not None:
                value = re.sub(en_val, "_EN_", value)
        logger.debug("Setting {0} to {1} under profiles {2}"
                     .format(key, value, profiles))
        if key not in self.property_dict:
            if value == '':
                pass
            elif None in profiles:
                self.property_dict[key] = {value: set([None])}
            else:
                self.property_dict[key] = {value: profiles}
        else:
            for (known_value, profile_set) in list(
                    self.property_dict[key].items()):
                if not overwrite and profile_set.intersection(profiles):
                    raise OVFItemDataError(
                        "Tried to set value:\n'{0}'\nfor property\n'{1}'\n"
                        "under profile(s) {2} but already had value:\n'{3}'\n"
                        "for this property under profile(s) {4}"
                        .format(value, key, profiles,
                                known_value,
                                profile_set.intersection(profiles)))
                if known_value != value:
                    # Our profiles should not use this old value
                    profile_set -= profiles
                    if not profile_set:
                        logger.debug("No longer any profiles with value {0}"
                                     .format(known_value))
                        del self.property_dict[key][known_value]
                else:
                    # Add our profiles to the existing set using this value
                    if None in profile_set:
                        # No need to add ourselves, we're already covered
                        # implicitly by the default
                        pass
                    elif None in profiles:
                        # Can remove all others currently in the set, as
                        # default will cover them implicitly
                        profile_set.clear()
                        profile_set.add(None)
                    else:
                        profile_set |= profiles
            if value != '' and value not in self.property_dict[key].keys():
                if None in profiles:
                    self.property_dict[key][value] = set([None])
                else:
                    self.property_dict[key][value] = profiles
            elif not self.property_dict[key]:
                logger.debug("No longer any values saved for {0}"
                             .format(key))
                del self.property_dict[key]
        self.modified = True
        self.validate()

    def add_profile(self, new_profile, from_item=None):
        """Adds a new profile for this item based on the default values
        from this item or another item.
        """
        if self.has_profile(new_profile):
            logger.error("Profile {0} already exists under {1}!"
                         .format(new_profile, self))
            return
        if from_item is None:
            from_item = self
        logger.debug("Adding profile {0} to item {1} from item {2}"
                     .format(new_profile,
                             self.property_dict.get(self.INSTANCE_ID,
                                                    "<unknown instance>"),
                             from_item.property_dict[self.INSTANCE_ID]))
        p_set = set([new_profile])
        for key in from_item.property_dict.keys():
            found = False
            if not from_item.property_dict[key]:
                logger.debug("No values stored for key {0} - not cloning it"
                             .format(key))
                continue
            for (value, profiles) in from_item.property_dict[key].items():
                if (None in profiles or
                        len(from_item.property_dict[key].keys()) == 1):
                    self.set_property(key, value, p_set)
                    found = True
                    break
            if not found:
                raise RuntimeError(
                    "Not sure which value to clone for {0}: {1}"
                    .format(key, from_item.property_dict[key].items()))
        self.modified = True
        self.validate()

    def remove_profile(self, profile):
        """Remove all trace of the given profile from this item"""
        if not self.has_profile(profile):
            logger.error("Requested deletion of profile '{0}' but it is "
                         "not present under {1}!"
                         .format(profile, self))
            return
        logger.debug("Removing profile {0} from item {1}"
                     .format(profile, self.property_dict[self.INSTANCE_ID]))
        p_set = set([profile])
        for key in self.property_dict.keys():
            for (value, profiles) in list(self.property_dict[key].items()):
                profiles -= p_set
                # Convert "any profile" to a list of all profiles minus
                # this one and any profiles already set elsewhere
                if None in profiles:
                    logger.debug("Profile contains 'any profile'; "
                                 "fixing it up")
                    profiles.update(self.ovf.get_configuration_profile_ids())
                    profiles.discard(None)
                    profiles.discard(profile)
                    # Discard all profiles set elsewhere
                    for (v, p) in list(self.property_dict[key].items()):
                        if v == value:
                            continue
                        profiles -= p
                    logger.debug("profiles are now: {0}".format(profiles))
                if not profiles:
                    del self.property_dict[key][value]
        self.modified = True
        self.validate()

    def get(self, tag):
        """Gets the dict associated with the given XML tag, if any.
        """
        return self.property_dict.get(tag, None)

    def get_value_internal(self, tag, profiles=None):
        """Get the internal value string (wildcard tags and temporary
        substitutions included) associated with the given profiles
        for the given tag. If the tag does not exist under these profiles, or
        the tag values differ across the profiles, returns None.

        DO NOT USE OUTSIDE THE OVFHardware CLASS - USE get_value() INSTEAD!
        """
        if profiles is not None:
            profiles = set(profiles)
        val_dict = self.property_dict.get(tag, {})
        if profiles is None:
            if len(val_dict) == 1:
                return list(val_dict.keys())[0]
            else:
                return None
        # A case we need to handle:
        # {'1': set([None])
        #  '4': set(['x'])
        # get_value([None, 'y', 'z'])  --> return '1'
        # get_value([None, 'x']) --> return None
        # We have to recognize that y and z are implicit in None but z is not.
        default_val = None
        for (val, prof) in val_dict.items():
            if prof.issuperset(profiles):
                return val
            if None in prof:
                default_val = val
            elif not prof.isdisjoint(profiles):
                return None
        return default_val

    def get_value(self, tag, profiles=None):
        """Gets the value string associated with the given profiles for
        the given tag. If the tag does not exist under these profiles, or the
        tag values differ across the profiles, returns None.
        """
        val = self.get_value_internal(tag, profiles)

        if val:
            # To regenerate text that depends on these values:
            rst_val = self.get_value_internal(self.RESOURCE_SUB_TYPE, profiles)
            vq_val = self.get_value_internal(self.VIRTUAL_QUANTITY, profiles)
            en_val = self.get_value_internal(self.ELEMENT_NAME, profiles)
            if rst_val is not None:
                val = re.sub("_RST_", str(rst_val), str(val))
            if vq_val is not None:
                val = re.sub("_VQ_", str(vq_val), str(val))
            if en_val is not None:
                val = re.sub("_EN_", str(en_val), str(val))

        return val

    def get_all_values(self, tag):
        """Gets the set of all value strings for the given tag.
        """
        return set(self.property_dict.get(tag, {}).keys())

    def validate(self):
        """Verify that the OVFItem describes a valid set of items"""
        # An OVFItem must describe only one InstanceID
        # All Items with a given InstanceID must have the same ResourceType
        for key in [self.INSTANCE_ID, self.RESOURCE_TYPE]:
            if len(self.property_dict.get(key, {})) > 1:
                raise RuntimeError("OVFItem illegally contains multiple {0} "
                                   "values: {1}"
                                   .format(key,
                                           self.property_dict[key].keys()))
        for (key, value_dict) in self.property_dict.items():
            set_so_far = set()
            for profile_set in value_dict.values():
                if None in profile_set and len(profile_set) > 1:
                    logger.warning("Profile set {0} contains redundant info; "
                                   "cleaning it up now..."
                                   .format(profile_set))
                    # Clean up...
                    profile_set.clear()
                    profile_set.add(None)
                # Make sure the profile sets are mutually exclusive
                inter = set_so_far.intersection(profile_set)
                if inter:
                    raise RuntimeError("OVFItem illegally contains duplicate "
                                       "profiles {0} under {1}: {2}"
                                       .format(inter, key, value_dict))
                set_so_far |= profile_set

    def has_profile(self, profile):
        """Returns whether this Item exists under the given profile"""
        instance_dict = self.property_dict.get(self.INSTANCE_ID, None)
        if not instance_dict:
            return False
        profile_set = set.union(*instance_dict.values())
        if profile in profile_set:
            return True
        elif (None in profile_set and
              profile in self.ovf.get_configuration_profile_ids()):
            return True
        return False

    def generate_items(self):
        """A list of Item XML elements derived from this object's data"""

        # First step - identify the minimal non-intersecting set of profiles
        set_list = []
        for key in self.property_dict.keys():
            for (val, new_set) in self.property_dict[key].items():
                new_list = []
                for existing_set in set_list:
                    # If the sets are identical or do not intersect, do nothing
                    if (new_set == existing_set or
                            not new_set.intersection(existing_set)):
                        if existing_set and existing_set not in new_list:
                            new_list.append(existing_set)
                        continue
                    # Otherwise, need to re-partition!
                    set_a = existing_set.difference(new_set)
                    if set_a and set_a not in new_list:
                        new_list.append(set_a)

                    set_b = existing_set.intersection(new_set)
                    if set_b and set_b not in new_list:
                        new_list.append(set_b)

                    new_set = new_set.difference(existing_set)

                if new_set and new_set not in new_list:
                    new_list.append(new_set)
                set_list = new_list

        logger.debug("Final set list is {0}".format(set_list))

        # Construct a list of profile strings
        set_string_list = []
        for final_set in set_list:
            if None in final_set:
                set_string_list.append("")
            else:
                set_string_list.append(" ".join(natural_sort(final_set)))
        set_string_list = natural_sort(set_string_list)
        logger.debug("set string list: {0}".format(set_string_list))

        # Now, construct the Items
        if self.NS == self.RASD:
            ITEM = self.ITEM
        elif self.NS == self.SASD:
            ITEM = self.STORAGE_ITEM
        elif self.NS == self.EPASD:
            ITEM = self.ETHERNET_PORT_ITEM
        else:
            raise ValueUnsupportedError("namespace",
                                        self.NS,
                                        [self.RASD, self.SASD, self.EPASD])
        child_ordering = [self.NS + i for i in self.ITEM_CHILDREN]
        item_list = []
        for set_string in set_string_list:
            if not set_string:
                # no config profile
                item = ET.Element(ITEM)
                final_set = set([None])
            else:
                item = ET.Element(ITEM, {self.ITEM_CONFIG: set_string})
                final_set = set(set_string.split())
            logger.debug("set string: {0}; final_set: {1}"
                         .format(set_string, final_set))
            # To regenerate text that depends on these values:
            rst_val = self.get_value(self.RESOURCE_SUB_TYPE, final_set)
            vq_val = self.get_value(self.VIRTUAL_QUANTITY, final_set)
            en_val = self.get_value(self.ELEMENT_NAME, final_set)
            for key in sorted(self.property_dict.keys()):
                default_val = None
                found = False
                for (val, val_set) in self.property_dict[key].items():
                    if final_set.issubset(val_set):
                        found = True
                        break
                    elif None in val_set:
                        default_val = val
                if not found:
                    if default_val is None:
                        logger.warning("Attribute '{0}' not found under '{1}'"
                                       .format(key, set_string))
                        continue
                    val = default_val
                # Regenerate text that depends on the VirtualQuantity
                # or ResourceSubType strings:
                if key == self.ELEMENT_NAME or key == self.ITEM_DESCRIPTION:
                    if rst_val is not None:
                        val = re.sub("_RST_", str(rst_val), str(val))
                    if vq_val is not None:
                        val = re.sub("_VQ_", str(vq_val), str(val))
                if key == self.ITEM_DESCRIPTION:
                    if en_val is not None:
                        val = re.sub("_EN_", str(en_val), str(val))

                # Is this an attribute, a child, or a custom element?
                attrib_match = re.match("(.*)" + self.ATTRIB_KEY_SUFFIX, key)
                if attrib_match:
                    attrib_string = attrib_match.group(1)
                child_attrib = re.match("(.*)_attrib_(.*)", key)
                custom_elem = re.match("(.*)" + self.ELEMENT_KEY_SUFFIX, key)
                if attrib_match:
                    item.set(attrib_string, val)
                elif child_attrib:
                    child = XML.set_or_make_child(
                        item,
                        child_attrib.group(1),
                        None,
                        ordering=child_ordering,
                        known_namespaces=self.NSM.values())
                    child.set(child_attrib.group(2), val)
                elif custom_elem:
                    # Recreate the element in question and append it
                    item.append(ET.fromstring(val))
                else:
                    # Children of Item must be in sorted order
                    XML.set_or_make_child(item, self.NS + key, val,
                                          ordering=child_ordering,
                                          known_namespaces=self.NSM.values())
            logger.debug("Item is:\n{0}".format(ET.tostring(item)))
            item_list.append(item)

        return item_list
