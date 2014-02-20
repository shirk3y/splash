# -*- coding: utf-8 -*-
"""
Classes that process (and maybe filter) requests based on
various conditions. They should be used with
``splash.network_manager.SplashQNetworkAccessManager``.
"""
from __future__ import absolute_import
import re
import os
import urlparse
from PyQt4.QtCore import QUrl
from PyQt4.QtNetwork import QNetworkAccessManager
from splash.utils import getarg, qurl2ascii
from twisted.python import log


OPERATION_NAMES = {
    QNetworkAccessManager.HeadOperation: 'HEAD',
    QNetworkAccessManager.GetOperation: 'GET',
    QNetworkAccessManager.PostOperation: 'POST',
    QNetworkAccessManager.PutOperation: 'PUT',
    QNetworkAccessManager.DeleteOperation: 'DELETE',
}


def _drop_request(request):
    # hack: set invalid URL
    request.setUrl(QUrl(''))


def request_repr(request, operation=None):
    method = OPERATION_NAMES.get(operation, '?')
    url = qurl2ascii(request.url())
    return "%s %s" % (method, url)


class AllowedDomainsMiddleware(object):
    """
    This request middleware checks ``allowed_domains`` GET argument
    and drops all requests to domains not in ``allowed_domains``.
    """
    def __init__(self, allow_subdomains=True, verbosity=0):
        self.allow_subdomains = allow_subdomains
        self.verbosity = verbosity

    def process(self, request, splash_request, operation, data):
        allowed_domains = self._get_allowed_domains(splash_request)
        host_re = self._get_host_regex(allowed_domains, self.allow_subdomains)
        if not host_re.match(unicode(request.url().host())):
            if self.verbosity >= 2:
                log.msg("Dropped offsite %s" % (request_repr(request, operation),), system='request_middleware')
            _drop_request(request)
        return request

    def _get_allowed_domains(self, splash_request):
        allowed_domains = getarg(splash_request, "allowed_domains", None)
        if allowed_domains is not None:
            return allowed_domains.split(',')

    def _get_host_regex(self, allowed_domains, allow_subdomains):
        """ Override this method to implement a different offsite policy """
        if not allowed_domains:
            return re.compile('')  # allow all by default
        domains = [d.replace('.', r'\.') for d in allowed_domains]
        if allow_subdomains:
            regex = r'(.*\.)?(%s)$' % '|'.join(domains)
        else:
            regex = r'(%s)$' % '|'.join(domains)
        return re.compile(regex, re.IGNORECASE)


class RequestLoggingMiddleware(object):
    """ Request middleware for logging requests """
    def process(self, request, splash_request, operation, data):
        log.msg(
            "Request %s %s" % (id(splash_request), request_repr(request, operation)),
            system='network'
        )
        return request


class AdblockMiddleware(object):
    """ Request middleware that discards requests based on Adblock rules """

    def __init__(self, rules_registry, verbosity=0):
        self.rules = rules_registry
        self.verbosity = verbosity

    def process(self, request, splash_request, operation, data):
        filter_names = [f for f in getarg(splash_request, "filters", default="").split(',') if f]

        if filter_names == ['none']:
            return request

        if not filter_names:
            if self.rules.filter_is_known('default'):
                filter_names = ['default']
            else:
                return request

        url, options = self._url_and_options(request, splash_request)
        blocking_filter = self.rules.get_blocking_filter(filter_names, url, options)
        if blocking_filter:
            if self.verbosity >= 2:
                msg = "Filter %s: dropped %s %s" % (
                    blocking_filter,
                    id(splash_request),
                    request_repr(request, operation)
                )
                log.msg(msg, system='request_middleware')
            _drop_request(request)
        return request

    def _url_and_options(self, request, splash_request):
        url = unicode(request.url().toString())
        domain = urlparse.urlsplit(getarg(splash_request, 'url')).netloc
        options = {'domain': domain}
        return url, options


class RulesRegistry(object):
    def __init__(self, path, supported_options=('domain',), verbosity=0):
        self.filters = {}
        self.verbosity = verbosity
        self.supported_options = supported_options
        self._load(path)

    def get_blocking_filter(self, filter_names, url, options):
        for name in filter_names:
            if name not in self.filters:
                if self.verbosity >= 1:
                    # this shouldn't happen because filter
                    # names must be validated earlier
                    log.msg("Invalid filter name: %s" % name)

        for name in filter_names:
            if name not in self.filters:
                continue
            if self.filters[name].should_block(url, options):
                return name

    def _load(self, path):
        import adblockparser

        for fname in os.listdir(path):
            if not fname.endswith('.txt'):
                continue
            fpath = os.path.join(path, fname)
            name = fname[:-len('.txt')]

            if not os.path.isfile(fpath):
                continue

            if self.verbosity >= 1:
                log.msg("Loading filters: %s" % fname)

            with open(fpath, 'rt') as f:
                lines = [line.decode('utf8').strip() for line in f]

            self.filters[name] = adblockparser.AdblockRules(
                lines,
                supported_options=self.supported_options,
                skip_unsupported_rules=False,
                max_mem=512*1024*1024,  # this doesn't actually use 512M
            )

    def filter_is_known(self, name):
        return name in self.filters