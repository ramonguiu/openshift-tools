#!/usr/bin/python
''' script to report on expiration dates (in days) for
    certificate expiration '''

# Reason: disable invalid-name because pylint does not like our naming convention
# pylint: disable=invalid-name

import os
import re
import argparse
import datetime
from stat import S_ISDIR, S_ISREG
from dateutil import parser
import OpenSSL.crypto

# Reason: disable pylint import-error because our libs aren't loaded on jenkins.
# Status: temporary until we start testing in a container where our stuff is ins talled.
# pylint: disable=import-error
from openshift_tools.monitoring.metric_sender import MetricSender



CERT_DISC_KEY = 'disc.certificate.expiration'
CERT_DISC_MACRO = '#OSO_CERT_NAME'

class CertificateReporting(object):
    ''' class with ability to parse through x509 certificates to extract
        and report to zabbix the expiration date assocated with the cert '''

    def __init__(self):
        ''' constructor '''
        self.args = None
        self.current_date = datetime.datetime.today()
        self.parse_args()
        self.msend = MetricSender(debug=self.args.debug)
        self.days_left_internal = None
        self.days_left_external = None

    def dprint(self, msg):
        ''' debug printer '''

        if self.args.debug:
            print msg

    def parse_args(self):
        ''' parse command line args '''
        argparser = argparse.ArgumentParser(description='certificate checker')
        argparser.add_argument('--debug', default=False, action='store_true')
        argparser.add_argument('--cert-list', default="/etc/origin", type=str,
                               help='comma-separated list of dirs/certificates')
        self.args = argparser.parse_args()

    def days_to_expiration(self, cert_file):
        ''' return days to expiration for a certificate '''

        crypto = OpenSSL.crypto

        cert = open(cert_file).read()
        certificate = crypto.load_certificate(crypto.FILETYPE_PEM, cert)
        expiration_date_asn1 = certificate.get_notAfter()
        # expiration returned in ASN.1 GENERALIZEDTIME format
        # YYYYMMDDhhmmss with a trailing 'Z'
        expiration_date = parser.parse(expiration_date_asn1).replace(tzinfo=None)

        delta = expiration_date - self.current_date
        return delta.days

    def set_days_left(self, cert_file, days_left):
        ''' set days left per cert type to closest to now '''

        if self.openshift_cert_issuer(cert_file):
            if (self.days_left_internal > days_left) or (self.days_left_internal is None):
                self.days_left_internal = days_left
            #self.dprint("{} days left on internal certs".format(self.days_left_internal))
        else:
            if (self.days_left_external > days_left) or (self.days_left_external is None):
                self.days_left_external = days_left
            #self.dprint("{} days left on external certs".format(self.days_left_external))

    def process_certificates(self):
        ''' check through list of certificates/directories '''

        for cert in self.args.cert_list.split(','):
            if not os.path.exists(cert):
                self.dprint("{} does not exist. skipping.".format(cert))
                continue

            mode = os.stat(cert).st_mode
            if S_ISDIR(mode):
                self.all_certs_in_dir(cert)
            elif S_ISREG(mode):
                days = self.days_to_expiration(cert)
                self.set_days_left(cert, days)
                self.dprint("{} in {} days".format(cert, days))
            else:
                self.dprint("not a file. not a directory. skipping.")

        self.dprint("{} days left on internal certs".format(self.days_left_internal))
        if self.days_left_internal != None:
            self.add_metrics("internal", self.days_left_internal)

        self.dprint("{} days left on external certs".format(self.days_left_external))
        if self.days_left_external != None:
            self.add_metrics("external", self.days_left_external)

        # now push out all queued up item(s) to metric servers
        #self.msend.send_metrics()

    def add_metrics(self, certtype, days_to_expiration):
        ''' queue up item for submission to zabbix '''

        self.msend.add_dynamic_metric(CERT_DISC_KEY, CERT_DISC_MACRO, [certtype])
        zbx_key = "{}[{}]".format(CERT_DISC_KEY, certtype)
        self.msend.add_metric({zbx_key: days_to_expiration})

    def all_certs_in_dir(self, directory):
        ''' recursively go through all *.crt files in 'directory' '''

        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                if filename.endswith('.crt'):
                    full_path = os.path.join(root, filename)
                    days = self.days_to_expiration(full_path)
                    self.set_days_left(full_path, days)
                    self.dprint("{} in {} days".format(full_path, days))

    def openshift_cert_issuer(self, cert_file):
        ''' return internal if certificate is issued by an openshift signer otherwise external '''

        crypto = OpenSSL.crypto

        ## openshift CA matches e.g. etcd-issuer@12345678 or openshift-issuer@12345678
        openshift_issuer_match = re.compile('.*-signer@[0-9]{10}$')

        cert = open(cert_file).read()
        certificate = crypto.load_certificate(crypto.FILETYPE_PEM, cert)
        issuer = certificate.get_issuer().CN

        match = openshift_issuer_match.match(issuer)

        if match:
            self.dprint("{} type internal".format(cert_file))
            return True
        else:
            self.dprint("{} type external".format(cert_file))
            return False

if __name__ == '__main__':
    CertificateReporting().process_certificates()
