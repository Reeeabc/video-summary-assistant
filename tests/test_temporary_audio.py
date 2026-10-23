import hashlib
import base64
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

import temporary_audio as upload
import video_assistant as app


DATA = b'audio-only-fixture'
MD5 = hashlib.md5(DATA).hexdigest()
POLICY = {'upload_host': 'https://temporary.oss-cn-beijing.aliyuncs.com',
          'upload_dir': 'dashscope-instant/test', 'oss_access_key_id': 'private-key',
          'signature': 'private-signature', 'policy': base64.b64encode(json.dumps({'conditions': [
              ['content-length-range', 0, 1024]]}).encode()).decode(),
          'x_oss_object_acl': 'private', 'x_oss_forbid_overwrite': 'true'}


class Bytes(httpx.SyncByteStream):
    def __init__(self, value=DATA): self.value = value
    def __iter__(self): yield self.value


class TemporaryAudioTests(unittest.TestCase):
    def test_akamai_file_endpoint_uses_bounded_ranges_with_public_media_headers(self):
        calls = []
        def media(request):
            calls.append(request)
            self.assertEqual(request.headers['range'],'bytes=0-1023')
            self.assertEqual(request.headers['referer'],'https://www.bilibili.com/')
            self.assertEqual(request.headers['connection'],'close')
            return httpx.Response(200,headers={'content-type':'application/octet-stream',
                'content-length':str(len(DATA))},stream=Bytes())
        with tempfile.TemporaryDirectory() as td,httpx.Client(transport=httpx.MockTransport(media)) as client:
            target=Path(td)/'audio.m4a'
            result=upload.download_audio(client,'https://upos-hz-mirrorakam.akamaized.net/audio',
                {'referer':'https://www.bilibili.com/'},target,1024)
            self.assertEqual(target.read_bytes(),DATA)
            self.assertEqual(result['download_http_status'],200)
            self.assertTrue(result['complete_file_verified'])
        self.assertEqual(len(calls),1)

    def test_download_is_bounded_complete_and_does_not_forward_auth(self):
        def media(request):
            self.assertEqual(request.headers['user-agent'], 'browser')
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            self.assertEqual(request.headers['range'], 'bytes=0-1023')
            return httpx.Response(200, headers={'content-type': 'video/mp4',
                'content-length': str(len(DATA))}, stream=Bytes())
        with tempfile.TemporaryDirectory() as td, httpx.Client(transport=httpx.MockTransport(media)) as client:
            path = Path(td) / 'audio.m4a'
            result = upload.download_audio(client, 'https://cdn.test/audio?sign=private',
                {'User-Agent': 'browser', 'Cookie': 'private', 'Authorization': 'Bearer private'}, path, 1024)
            self.assertEqual(path.read_bytes(), DATA)
            self.assertEqual(result['md5'], MD5)
            self.assertEqual(result['sha256'], hashlib.sha256(DATA).hexdigest())

    def test_size_and_partial_response_fail_before_upload(self):
        for status, length, body in [(206, len(DATA), DATA), (200, 5000, DATA),
                                     (200, len(DATA) + 1, DATA), (200, len(DATA) - 1, DATA)]:
            with self.subTest(status=status, length=length), tempfile.TemporaryDirectory() as td:
                with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status,
                        headers={'content-type': 'audio/mp4', 'content-length': str(length)}, stream=Bytes(body)))) as client:
                    with self.assertRaises(upload.AudioUploadError):
                        upload.download_audio(client, 'https://cdn.test/audio', {}, Path(td) / 'a.m4a', 1024)

    def test_complete_range_response_is_downloaded_once_and_verified(self):
        calls = []
        def media(request):
            calls.append(request)
            self.assertEqual(request.headers['range'], 'bytes=0-1023')
            return httpx.Response(206, headers={'content-type':'video/mp4', 'content-length':str(len(DATA)),
                'content-range':f'bytes 0-{len(DATA)-1}/{len(DATA)}'}, stream=Bytes())
        with tempfile.TemporaryDirectory() as td, httpx.Client(transport=httpx.MockTransport(media)) as client:
            target = Path(td)/'audio.m4a'
            result = upload.download_audio(client, 'https://cdn.test/audio', {}, target, 1024)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertTrue(result['complete_file_verified'])
            self.assertEqual(result['download_http_status'], 206)
            self.assertEqual(result['md5'], MD5)
        self.assertEqual(len(calls), 1)

    def test_incomplete_malformed_or_inconsistent_ranges_stop_before_file_creation(self):
        for status, value in [(206,'bytes 0-9/100'), (206,'bytes 1-18/19'), (206,'bytes 0-17/*'),
                              (206,'bytes 0-17/17'), (206,'bytes 0-17/19'), (206,'bytes 0-17/999999'),
                              (200,'bytes 0-17/18')]:
            with self.subTest(status=status, value=value), tempfile.TemporaryDirectory() as td:
                with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(status,
                    headers={'content-type':'audio/mp4','content-length':str(len(DATA)),'content-range':value},stream=Bytes()))) as client:
                    target = Path(td)/'audio.m4a'
                    with self.assertRaises(upload.AudioUploadError):
                        upload.download_audio(client, 'https://cdn.test/audio', {}, target, 1024)
                    self.assertFalse(target.exists())

    def test_ordered_ranges_cover_the_whole_audio_and_keep_a_stable_validator(self):
        calls = []
        def media(request):
            calls.append(request.headers['range'])
            start, end = map(int, request.headers['range'][6:].split('-'))
            end = min(end, len(DATA)-1)
            self.assertNotIn('if-range', request.headers)
            self.assertEqual(request.headers['connection'], 'close')
            body = DATA[start:end+1]
            return httpx.Response(206, headers={'content-type':'audio/mp4','content-length':str(len(body)),
                'content-range':f'bytes {start}-{end}/{len(DATA)}','etag':'"v1"'},stream=Bytes(body))
        with tempfile.TemporaryDirectory() as td, patch.object(upload, 'DOWNLOAD_CHUNK_BYTES', 4), \
             httpx.Client(transport=httpx.MockTransport(media)) as client:
            target = Path(td)/'audio.m4a'
            result = upload.download_audio(client, 'https://cdn.test/audio', {}, target, 1024)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertEqual(result['md5'], MD5)
            self.assertTrue(result['range_validator_verified'])
            self.assertEqual(result['download_requests'], 5)
        self.assertEqual(calls, ['bytes=0-3','bytes=4-7','bytes=8-11','bytes=12-15','bytes=16-17'])

    def test_changed_missing_or_ignored_later_range_is_not_appended_or_retried(self):
        for failure in ('etag','offset','total','ignored','truncated'):
            calls = []
            def media(request):
                calls.append(request.headers['range'])
                first = len(calls) == 1
                start, end, total = (0, 3, len(DATA)) if first else (4, 7, len(DATA))
                if not first and failure == 'offset': start = 0
                if not first and failure == 'total': total += 1
                body = DATA[start:end+1]
                if not first and failure == 'truncated': body = body[:-1]
                return httpx.Response(200 if not first and failure == 'ignored' else 206,
                    headers={'content-type':'audio/mp4', 'content-length':str(end-start+1),
                        'content-range':f'bytes {start}-{end}/{total}',
                        'etag':'"v2"' if not first and failure == 'etag' else '"v1"'},stream=Bytes(body))
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td, \
                 patch.object(upload, 'DOWNLOAD_CHUNK_BYTES', 4), httpx.Client(transport=httpx.MockTransport(media)) as client:
                with self.assertRaises(upload.AudioUploadError):
                    upload.download_audio(client, 'https://cdn.test/audio', {}, Path(td)/'audio.m4a', 1024)
                self.assertEqual(len(calls), 2)

    def test_range_transport_failure_records_safe_progress_and_never_retries(self):
        calls = []
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.ReadTimeout('https://cdn.test/?signature=secret Cookie: private')
                yield b''
        def media(request):
            calls.append(request)
            first = len(calls) == 1
            return httpx.Response(206, headers={'content-type':'audio/mp4','content-length':'4',
                'content-range':f'bytes {0 if first else 4}-{3 if first else 7}/{len(DATA)}'},
                stream=Bytes(DATA[:4]) if first else Broken())
        with tempfile.TemporaryDirectory() as td, patch.object(upload, 'DOWNLOAD_CHUNK_BYTES', 4), \
             httpx.Client(transport=httpx.MockTransport(media)) as client:
            with self.assertRaises(upload.AudioUploadError) as caught:
                upload.download_audio(client, 'https://cdn.test/audio', {}, Path(td)/'audio.m4a', 1024)
            self.assertEqual(caught.exception.details['network_error'], 'ReadTimeout')
            self.assertEqual(caught.exception.details['bytes_received'], 4)
            self.assertEqual(caught.exception.details['responses_received'], 3)
            self.assertEqual(caught.exception.details['network_retries'], 1)
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(calls), 3)

    def test_retry_rolls_back_partial_bytes_and_hashes_before_repeating_the_same_range(self):
        payload = b'a' * (3 * 1024 * 1024)
        calls = []
        class Partial(httpx.SyncByteStream):
            def __iter__(self):
                yield b'X' * 65536
                raise httpx.ReadTimeout('private')
        def media(request):
            calls.append(request.headers['range'])
            start,end=map(int,request.headers['range'][6:].split('-'))
            end=min(end,len(payload)-1)
            return httpx.Response(206,headers={'content-type':'audio/mp4','content-length':str(end-start+1),
                'content-range':f'bytes {start}-{end}/{len(payload)}','etag':'"same"'},
                stream=Partial() if len(calls)==2 else Bytes(payload[start:end+1]))
        with tempfile.TemporaryDirectory() as td,patch.object(upload,'DOWNLOAD_CHUNK_BYTES',131072), \
             httpx.Client(transport=httpx.MockTransport(media)) as client:
            target=Path(td)/'audio.m4a'
            result=upload.download_audio(client,'https://cdn.test/audio',{},target,len(payload))
            self.assertEqual(target.read_bytes(),payload)
            self.assertEqual(result['sha256'],hashlib.sha256(payload).hexdigest())
            self.assertEqual(result['md5'],hashlib.md5(payload).hexdigest())
            self.assertEqual(result['download_network_retries'],1)
        self.assertEqual(calls[0],'bytes=0-32767')
        self.assertEqual(calls[1:3],['bytes=32768-163839','bytes=32768-163839'])

    def test_small_tail_ranges_avoid_large_eof_response_truncation_without_losing_bytes(self):
        payload=b'a'*(2*1024*1024+400000)
        tail_start=len(payload)-800000
        calls=[]
        def media(request):
            start,end=map(int,request.headers['range'][6:].split('-'))
            end=min(end,len(payload)-1)
            calls.append((start,end))
            if end>=tail_start:self.assertLessEqual(end-start+1,16384)
            body=payload[start:end+1]
            return httpx.Response(206,headers={'content-type':'audio/mp4','content-length':str(len(body)),
                'content-range':f'bytes {start}-{end}/{len(payload)}'},stream=Bytes(body))
        with tempfile.TemporaryDirectory() as td,httpx.Client(transport=httpx.MockTransport(media)) as client:
            target=Path(td)/'audio.m4a'
            result=upload.download_audio(client,'https://cdn.test/audio',{},target,len(payload))
            self.assertEqual(target.read_bytes(),payload)
            self.assertEqual(result['download_network_retries'],0)
            self.assertEqual(result['sha256'],hashlib.sha256(payload).hexdigest())
        self.assertEqual(calls[-1][1],len(payload)-1)

    def test_transport_retry_budget_is_three_for_the_entire_file(self):
        seen,calls=set(),[]
        def media(request):
            calls.append(request.headers['range'])
            start,end=map(int,request.headers['range'][6:].split('-'))
            if start not in seen:
                seen.add(start)
                raise httpx.ReadTimeout('private')
            end=min(end,len(DATA)-1)
            return httpx.Response(206,headers={'content-type':'audio/mp4','content-length':str(end-start+1),
                'content-range':f'bytes {start}-{end}/{len(DATA)}'},stream=Bytes(DATA[start:end+1]))
        with tempfile.TemporaryDirectory() as td,patch.object(upload,'DOWNLOAD_CHUNK_BYTES',4), \
             httpx.Client(transport=httpx.MockTransport(media)) as client:
            with self.assertRaises(upload.AudioUploadError) as caught:
                upload.download_audio(client,'https://cdn.test/audio',{},Path(td)/'audio.m4a',1024)
            self.assertEqual(caught.exception.details['network_retries'],3)
            self.assertEqual(caught.exception.details['bytes_received'],12)
        self.assertEqual(len(calls),7)

    def inspect(self, streams, duration='10', decode_code=0):
        probe = subprocess.CompletedProcess([], 0, json.dumps({'streams': streams, 'format': {'duration': duration}}), '')
        decode = subprocess.CompletedProcess([], decode_code, '', '' if not decode_code else 'corrupt')
        with patch.object(upload.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(upload.subprocess, 'run', side_effect=[probe, decode]) as run:
            result = upload.validate_audio(Path('/tmp/audio.m4a'), 10)
            self.assertNotIn('DASHSCOPE_API_KEY', run.call_args.kwargs['env'])
            self.assertIn('file,pipe', run.call_args.args[0])
            return result

    def test_duration_and_full_decode_are_required(self):
        result = self.inspect([{'codec_type': 'audio', 'codec_name': 'aac', 'duration': '10'}])
        self.assertTrue(result['full_decode_verified'])
        for streams, duration, decode in [
            ([{'codec_type': 'video', 'codec_name': 'h264'}], '10', 0),
            ([{'codec_type': 'audio', 'codec_name': 'aac'}], '99', 0),
            ([{'codec_type': 'audio', 'codec_name': 'aac'}], '10', 1),
            ([{'codec_type': 'audio', 'codec_name': 'aac'}, {'codec_type': 'video'}], '10', 0)]:
            with self.subTest(streams=streams, duration=duration, decode=decode), self.assertRaises(upload.AudioUploadError):
                self.inspect(streams, duration, decode)

    def test_policy_is_model_scoped_and_untrusted_hosts_are_rejected(self):
        def api(request):
            self.assertEqual(request.url.path, '/api/v1/uploads')
            self.assertEqual(request.url.params['model'], 'qwen-audio-3.1-asr-flash-filetrans')
            self.assertEqual(request.url.params['action'], 'getPolicy')
            self.assertEqual(request.headers['Authorization'], 'Bearer test-secret')
            return httpx.Response(200, json={'data': POLICY})
        with httpx.Client(transport=httpx.MockTransport(api)) as client:
            self.assertEqual(upload.get_policy(client, 'https://dashscope.aliyuncs.com',
                {'Authorization': 'Bearer test-secret'}, 'qwen-audio-3.1-asr-flash-filetrans'), POLICY)
        for host in ['http://temporary.oss-cn-beijing.aliyuncs.com', 'https://aliyuncs.com.evil.test',
                     'https://user:password@temporary.aliyuncs.com', 'https://temporary.aliyuncs.com/?token=private']:
            with self.subTest(host=host), httpx.Client(transport=httpx.MockTransport(lambda r:
                    httpx.Response(200, json={'data': {**POLICY, 'upload_host': host}}))) as client:
                with self.assertRaises(upload.AudioUploadError):
                    upload.get_policy(client, 'https://dashscope.aliyuncs.com', {}, 'model')

    def test_upload_uses_form_credentials_not_api_authorization_and_checks_etag(self):
        def storage(request):
            self.assertNotIn('authorization', request.headers)
            self.assertIn(b'private-signature', request.content)
            self.assertIn(DATA, request.content)
            return httpx.Response(200, headers={'etag': '"' + MD5 + '"'})
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'audio.m4a'
            path.write_bytes(DATA)
            with httpx.Client(transport=httpx.MockTransport(storage)) as client:
                uri, receipt = upload.upload_audio(client, POLICY, path, MD5)
            self.assertTrue(uri.startswith('oss://'))
            self.assertTrue(receipt['server_checksum_verified'])
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={'etag': 'bad'}))) as client:
                with self.assertRaisesRegex(upload.AudioUploadError, 'checksum mismatch'):
                    upload.upload_audio(client, POLICY, path, MD5)

    def test_upload_timeout_is_unknown_and_never_retried(self):
        calls = []
        def failed(request):
            calls.append(request)
            raise httpx.ReadTimeout('private URL and credentials')
        with tempfile.TemporaryDirectory() as td, httpx.Client(transport=httpx.MockTransport(failed)) as client:
            path = Path(td) / 'audio.m4a'
            path.write_bytes(DATA)
            with self.assertRaisesRegex(upload.AudioUploadError, 'result unknown') as error:
                upload.upload_audio(client, POLICY, path, MD5)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('credentials', str(error.exception))

    def test_smaller_server_limit_is_reported_before_upload_without_workaround(self):
        small = {**POLICY, 'policy': base64.b64encode(json.dumps({'conditions': [
            ['content-length-range', 0, 8]]}).encode()).decode()}
        with tempfile.TemporaryDirectory() as td, httpx.Client(transport=httpx.MockTransport(
                lambda request: self.fail('must not attempt oversized upload'))) as client:
            path = Path(td) / 'audio.m4a'
            path.write_bytes(DATA)
            with self.assertRaises(upload.AudioUploadError) as caught:
                upload.upload_audio(client, small, path, MD5)
            self.assertEqual(caught.exception.details['server_max_bytes'], 8)
            self.assertEqual(caught.exception.details['file_bytes'], len(DATA))

    def test_upload_check_has_no_asr_route_and_removes_local_audio(self):
        calls, local_paths = [], []
        real_client = httpx.Client
        def handler(request):
            calls.append((request.method, request.url.host, request.url.path))
            if request.url.host == 'cdn.test':
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, headers={'content-type': 'audio/mp4',
                    'content-length': str(len(DATA))}, stream=Bytes())
            if request.url.path == '/api/v1/uploads':
                return httpx.Response(200, json={'data': POLICY})
            if request.url.host == 'temporary.oss-cn-beijing.aliyuncs.com':
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, headers={'etag': MD5})
            self.fail('transcription or unexpected request attempted')
        def validated(path, duration):
            local_paths.append(path)
            self.assertTrue(path.exists())
            return {'duration_seconds': duration, 'full_decode_verified': True}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(upload.httpx, 'Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)), \
             patch.object(upload, 'validate_audio', side_effect=validated), \
             patch.object(app, 'credentials', return_value={'Authorization': 'Bearer test-secret'}), \
             patch.object(app, 'resolve', return_value=({'video_url': 'https://www.bilibili.com/video/BV1gmJX6SExT?p=1',
                'duration_seconds': 75}, 'https://cdn.test/audio?sign=private')), \
             patch.object(app, 'submit', side_effect=AssertionError('must not submit ASR')):
            folder = Path(td) / 'upload-check'
            args = SimpleNamespace(url='BV1gmJX6SExT', output=folder, max_minutes=60, max_audio_mib=1)
            app.upload_check(args)
            state = json.loads((folder / 'upload-state.json').read_text())
            self.assertEqual(state['upload_status'], 'UPLOADED')
            self.assertTrue(state['local_audio_removed'])
            self.assertFalse(state['transcription_submitted'])
            text = (folder / 'upload-state.json').read_text()
            for secret in ['oss://', 'private-signature', 'private-policy', 'sign=', 'test-secret']:
                self.assertNotIn(secret, text)
            with self.assertRaises(app.Problem):
                app.upload_check(args)
        self.assertEqual(len(calls), 3)
        self.assertFalse(local_paths[0].exists())

    def test_local_audio_removed_on_validation_failure_before_policy_or_upload(self):
        paths, events = [], []
        def invalid(path, duration):
            paths.append(path)
            raise upload.AudioUploadError('validation', 'invalid audio')
        with patch.object(upload, 'download_audio', return_value={}), patch.object(upload, 'validate_audio', side_effect=invalid), \
             patch.object(upload, 'get_policy', side_effect=AssertionError('must not request credentials')):
            with self.assertRaises(upload.AudioUploadError):
                upload.stage_audio('https://cdn.test/audio', {}, 1, 'model', 'https://dashscope.aliyuncs.com', {},
                                   event=lambda status, details: events.append(status))
        self.assertFalse(paths[0].parent.exists())
        self.assertEqual(events[-1], 'LOCAL_AUDIO_REMOVED')

    def test_temporary_uri_submission_adds_required_resolve_header_only_when_selected(self):
        def accepted(request):
            self.assertEqual(request.headers['X-DashScope-OssResourceResolve'], 'enable')
            self.assertEqual(json.loads(request.content)['input']['file_urls'], ['oss://temporary/audio.m4a'])
            return httpx.Response(200, json={'output': {'task_id': 'test-task'}})
        with tempfile.TemporaryDirectory() as td, httpx.Client(transport=httpx.MockTransport(accepted)) as client:
            state = {'model': 'qwen-audio-3.1-asr-flash-filetrans', 'workspace': '', 'input_mode': 'bailian-upload'}
            app.submit(client, Path(td), state, 'oss://temporary/audio.m4a', {'Authorization': 'Bearer fake'})
            saved = json.loads((Path(td) / 'state.json').read_text())
            self.assertNotIn('oss://', json.dumps(saved))


if __name__ == '__main__':
    unittest.main()
