import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import media_http as media
import temporary_audio as upload


class Stream(httpx.SyncByteStream):
    def __init__(self, body):
        self.body, self.closed, self.iterated = body, False, False

    def __iter__(self):
        self.iterated = True
        yield self.body

    def close(self):
        self.closed = True


class MediaHTTPTests(unittest.TestCase):
    def check(self, response, max_bytes=1024):
        calls = []
        def handle(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            for name in ('authorization', 'cookie', 'referer', 'origin', 'range'):
                self.assertNotIn(name, request.headers)
            return response
        client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
        with patch.object(media.httpx, 'Client', return_value=client):
            try:
                return media.check_public_audio('https://cdn.test/audio?signature=private', max_bytes)
            finally:
                self.assertEqual(len(calls), 1)
                self.assertTrue(client.is_closed)

    def test_valid_full_get_closes_without_reading_audio_and_does_not_claim_provider_access(self):
        stream = Stream(b'audio')
        result = self.check(httpx.Response(200, headers={'content-type': 'audio/mp4', 'content-length': '5'}, stream=stream))
        self.assertFalse(stream.iterated)
        self.assertTrue(stream.closed)
        self.assertFalse(result['provider_access_verified'])
        self.assertEqual(result['media_body_bytes_read'], 0)

    def test_edgeone_error_is_bounded_and_never_exposes_raw_body_headers_or_url(self):
        secret = 'https://cdn.test/audio?signature=private Cookie: secret Authorization: Bearer secret'
        body = ('<html><style>' + ' ' * 4500 + '</style>Restricted Access. The security policy of this website '
                'has blocked you from further access.' + secret + ' ' * 9000).encode()
        stream = Stream(body)
        response = httpx.Response(567, headers={'content-type': 'text/html; charset=UTF-8',
            'content-length': str(len(body)), 'server': 'envoy', 'eo-log-uuid': 'abc012-Trace',
            'set-cookie': 'secret', 'location': secret, 'x-signature': 'secret'}, stream=stream)
        with self.assertRaises(media.MediaAccessError) as caught: self.check(response)
        details = caught.exception.details
        self.assertEqual(details['classification'], 'edgeone_security_block')
        self.assertEqual(details['edgeone_request_id'], 'abc012-Trace')
        self.assertIn('security_policy', details['body_markers'])
        self.assertEqual(details['error_body_bytes'], media.ERROR_BODY_LIMIT)
        for value in ('signature=', 'secret', 'Cookie:', '<html>', 'https://'):
            self.assertNotIn(value, str(caught.exception))
        self.assertTrue(stream.closed)

    def test_567_without_cdn_evidence_is_not_attributed_to_edgeone(self):
        with self.assertRaises(media.MediaAccessError) as caught:
            self.check(httpx.Response(567, text='unknown error', headers={'server':'envoy'}))
        self.assertEqual(caught.exception.details['classification'], 'http_rejected')

    def test_redirect_partial_or_non_file_response_never_becomes_a_success(self):
        for status, mime in ((302, 'audio/mp4'), (206, 'audio/mp4'), (200, 'text/html')):
            with self.subTest(status=status, mime=mime):
                stream = Stream(b'not a complete file')
                with self.assertRaises(media.MediaAccessError):
                    self.check(httpx.Response(status, headers={'content-type': mime,
                        'content-length':'19', 'location':'https://elsewhere.test/private'}, stream=stream))
                if mime == 'audio/mp4': self.assertFalse(stream.iterated)
                self.assertTrue(stream.closed)

    def test_missing_or_excessive_size_and_encoded_audio_stop_before_body_read(self):
        for extra in ({}, {'content-length':'2048'}, {'content-length':'0'},
                      {'content-length':'5', 'content-encoding':'gzip'}):
            with self.subTest(extra=extra):
                stream = Stream(b'audio')
                with self.assertRaises(media.MediaAccessError):
                    self.check(httpx.Response(200, headers={'content-type':'audio/mp4', **extra}, stream=stream))
                self.assertFalse(stream.iterated)

    def test_transport_failure_does_not_leak_exception_or_retry(self):
        def fail(request): raise httpx.ConnectError('secret signed URL', request=request)
        client = httpx.Client(transport=httpx.MockTransport(fail))
        with patch.object(media.httpx, 'Client', return_value=client), self.assertRaises(media.MediaAccessError) as caught:
            media.check_public_audio('https://cdn.test/audio', 1024)
        self.assertEqual(caught.exception.details, {'classification':'network_or_tls_failure'})

    def test_download_records_cdn_block_before_creating_audio_file(self):
        calls = []
        def blocked(request):
            calls.append(request)
            self.assertEqual(request.headers['referer'], 'https://www.bilibili.com/')
            return httpx.Response(567, text='Restricted Access: security policy of this website',
                headers={'eo-log-uuid':'abc-123', 'content-type':'text/html'})
        with tempfile.TemporaryDirectory() as td, httpx.Client(transport=httpx.MockTransport(blocked)) as client:
            path = Path(td)/'audio.m4a'
            with self.assertRaises(upload.AudioUploadError) as caught:
                upload.download_audio(client, 'https://cdn.test/audio', {'referer':'https://www.bilibili.com/'}, path, 1024)
            self.assertFalse(path.exists())
            self.assertEqual(caught.exception.details['classification'], 'edgeone_security_block')
            self.assertEqual(caught.exception.details['stage'], 'download')
        self.assertEqual(len(calls), 1)
