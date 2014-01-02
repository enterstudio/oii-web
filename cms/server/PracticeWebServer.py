#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2013-2014 Luca Versari <veluca93@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import io
import re
import json
import logging
import hashlib
import mimetypes
import traceback
import pkg_resources

from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy import desc

from cms import config, SOURCE_EXT_TO_LANGUAGE_MAP
from cms.log import initialize_logging
from cms.io.GeventLibrary import Service
from cms.io import ServiceCoord
from cms.db.filecacher import FileCacher
from cms.db import SessionGen, User, Submission, File, Task, Test, Tag, \
    Forum, Topic, Post, TestScore, Institute, Region, Province, City, \
    TaskScore, PrivateMessage
from cmscommon.DateTime import make_timestamp, make_datetime

from werkzeug.wrappers import Response, Request
from werkzeug.wsgi import SharedDataMiddleware, wrap_file, responder
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import HTTPException, NotFound, BadRequest, \
    Unauthorized

import gevent
import gevent.wsgi

logger = logging.getLogger(__name__)
local = gevent.local.local()


class WSGIHandler(gevent.wsgi.WSGIHandler):
    def format_request(self):
        if self.time_finish:
            delta = '%.6f' % (self.time_finish - self.time_start)
        else:
            delta = '-'
        client_address = self.environ['REMOTE_ADDR']
        return '%s %s %s %s' % (
            client_address or '-',
            (getattr(self, 'status', None) or '000').split()[0],
            delta,
            getattr(self, 'requestline', ''))

    def log_request(self):
        logger.info(self.format_request())

    def get_environ(self):
        env = gevent.wsgi.WSGIHandler.get_environ(self)
        # Proxy support
        if config.is_proxy_used:
            if 'HTTP_X_FORWARDED_FOR' in env:
                env['REMOTE_ADDR'] = \
                    env['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
            elif 'HTTP_X_REAL_IP' in env:
                env['REMOTE_ADDR'] = env['HTTP_X_REAL_IP']
        return env


class Server(gevent.wsgi.WSGIServer):
    handler_class = WSGIHandler


class APIHandler(object):
    def __init__(self, parent):
        self.router = Map([
            Rule('/', methods=['GET', 'POST'], endpoint='root'),
            Rule('/files/<digest>', methods=['GET', 'POST'],
                 endpoint='dbfile'),
            Rule('/files/<digest>/<name>', methods=['GET', 'POST'],
                 endpoint='dbfile'),
            Rule('/<target>', methods=['POST'], endpoint='jsondata')
        ], encoding_errors='strict')
        self.file_cacher = parent.file_cacher
        self.evaluation_service = parent.evaluation_service
        self.EMAIL_REG = re.compile(r'[^@]+@[^@]+\.[^@]+')
        self.USERNAME_REG = re.compile(r'^[A-Za-z0-9_\.]+$')

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)

    @responder
    def wsgi_app(self, environ, start_response):
        route = self.router.bind_to_environ(environ)
        try:
            endpoint, args = route.match()
        except HTTPException:
            return NotFound()

        try:
            if endpoint == 'root':
                return self.file_handler(environ, 'index.html')
            elif endpoint == 'dbfile':
                return self.dbfile_handler(environ, args)
        except HTTPException as e:
            return e

        request = Request(environ)
        if request.mimetype != 'application/json':
            logger.warning('Request not in JSON')
            data = dict()
        else:
            try:
                data = json.load(request.stream)
            except (ValueError, TypeError):
                logger.warning('JSON parse error')
                data = dict()
            if 'first' in data and 'last' in data:
                data['first'] = int(data['first'])
                data['last'] = int(data['last'])
                if data['first'] < 0 or data['first'] > data['last']:
                    return BadRequest()

        with SessionGen() as local.session:
            try:
                username = str(data['username'])
                token = str(data['token'])
                local.user = self.get_user(username, token)
            except (BadRequest, KeyError):
                local.user = None
            if local.user is None:
                local.access_level = 7  # Access level of unlogged user
            else:
                local.access_level = local.user.access_level

            try:
                local.data = data
                ans = getattr(self, args['target'] + '_handler')()
            except AttributeError:
                logger.error('Endpoint %s not implemented yet!' % endpoint)
                logger.error(traceback.format_exc())
                return NotFound()
            except KeyError:
                logger.error(traceback.format_exc())
                return BadRequest()
            except HTTPException as e:
                return e

        response = Response()
        response.mimetype = 'application/json'
        response.status_code = 200
        response.data = json.dumps(ans)
        return response

    # Useful methods
    def sliced_query(self, query):
        res = query.slice(local.data['first'], local.data['last']).all()
        num = query.count()
        return (res, num)

    def get_user(self, username, token):
        return local.session.query(User)\
            .filter(User.username == username)\
            .filter(User.password == token).first()

    def check_user(self, username):
        resp = dict()
        if len(username) < 4:
            resp['success'] = 0
            resp['error'] = 'signup.errors.username.short'
        elif not self.USERNAME_REG.match(username):
            resp['success'] = 0
            resp['error'] = 'signup.errors.username.invalid'
        else:
            user = local.session.query(User)\
                .filter(User.username == username).first()
            if user is None:
                resp['success'] = 1
            else:
                resp['success'] = 0
                resp['error'] = 'signup.errors.username.used'
        return resp

    def check_email(self, email):
        resp = dict()
        if not self.EMAIL_REG.match(email):
            resp['success'] = 0
            resp['error'] = 'signup.errors.email.invalid'
        else:
            user = local.session.query(User)\
                .filter(User.email == email).first()
            if user is None:
                resp['success'] = 1
            else:
                resp['success'] = 0
                resp['error'] = 'signup.errors.email.used'
        return resp

    def hash(self, string, algo='sha256'):
        if string is None:
            string = ''
        sha = getattr(hashlib, algo)()
        sha.update(string)
        return sha.hexdigest()

    def hashpw(self, pw):
        return self.hash(pw + config.secret_key)

    def get_institute_info(self, institute):
        info = dict()
        if institute is not None:
            info['id'] = institute.id
            info['name'] = institute.name
            info['city'] = institute.city.name
            info['province'] = institute.city.province.name
            info['region'] = institute.city.province.region.name
        return info

    def get_user_info(self, user):
        info = dict()
        info['username'] = user.username
        info['access_level'] = user.access_level
        info['join_date'] = make_timestamp(user.registration_time)
        info['mail_hash'] = self.hash(user.email, 'md5')
        info['post_count'] = len(user.posts)
        info['score'] = user.score
        info['institute'] = self.get_institute_info(user.institute)
        return info

    # Handlers that do not require JSON data
    def file_handler(self, environ, filename):
        path = os.path.join(
            pkg_resources.resource_filename('cms.web', 'practice'),
            filename)

        response = Response()
        response.status_code = 200
        response.mimetype = 'application/octect-stream'
        mimetype = mimetypes.guess_type(filename)[0]
        if mimetype is not None:
            response.mimetype = mimetype
        response.last_modified = \
            datetime.utcfromtimestamp(os.path.getmtime(path))\
                    .replace(microsecond=0)
        response.response = wrap_file(environ, io.open(path, 'rb'))
        response.direct_passthrough = True
        return response

    def dbfile_handler(self, environ, args):
        try:
            fobj = self.file_cacher.get_file(args['digest'])
        except KeyError:
            raise NotFound()

        response = Response()
        response.status_code = 200

        response.mimetype = 'application/octect-stream'
        if 'filename' in args:
            response.headers.add_header(
                b'Content-Disposition', b'attachment',
                filename=args['filename'])
            mimetype = mimetypes.guess_type(args['filename'])[0]
            if mimetype is not None:
                response.mimetype = mimetype

        response.response = wrap_file(environ, fobj)
        response.direct_passthrough = True
        return response

    # Handlers that require JSON data
    def check_handler(self):
        try:
            rtype = local.data['type']
            rvalue = local.data['value']
        except KeyError:
            logger.warning('Missing parameters')
            raise BadRequest()

        if rtype == 'username':
            return self.check_user(rvalue)
        elif rtype == 'email':
            return self.check_email(rvalue)

        logger.warning('Request type not understood')
        raise BadRequest()

    def location_handler(self):
        resp = dict()
        if local.data['action'] == 'get':
            institute = local.session.query(Institute)\
                .filter(Institute.id == local.data['id']).first()
            if institute is None:
                raise NotFound()
            resp = self.get_institute_info(institute)
        elif local.data['action'] == 'listregions':
            out = local.session.query(Region).all()
            resp['regions'] = [{'id': r.id, 'name': r.name} for r in out]
        elif local.data['action'] == 'listprovinces':
            out = local.session.query(Province)\
                .filter(Province.region_id == local.data['id']).all()
            resp['provinces'] = [{'id': r.id, 'name': r.name} for r in out]
        elif local.data['action'] == 'listcities':
            out = local.session.query(City)\
                .filter(City.province_id == local.data['id']).all()
            resp['cities'] = [{'id': r.id, 'name': r.name} for r in out]
        elif local.data['action'] == 'listinstitutes':
            out = local.session.query(Institute)\
                .filter(Institute.city_id == local.data['id']).all()
            resp['institutes'] = [{'id': r.id, 'name': r.name} for r in out]
        return resp

    def user_handler(self):
        resp = dict()
        if local.data['action'] == 'new':
            try:
                username = local.data['username']
                password = local.data['password']
                email = local.data['email']
                firstname = local.data['firstname']
                lastname = local.data['lastname']
                institute = int(local.data['institute'])
            except KeyError:
                logger.warning('Missing parameters')
                raise BadRequest()

            token = self.hashpw(password)

            resp = self.check_user(username)
            if not resp['success']:
                return resp
            resp = self.check_email(email)
            if not resp['success']:
                return resp

            resp['success'] = 1
            user = User(
                first_name=firstname,
                last_name=lastname,
                username=username,
                password=token,
                email=email,
                access_level=6,
                registration_time=make_datetime()
            )
            user.institute_id = institute
            try:
                local.session.add(user)
                local.session.commit()
            except IntegrityError:
                resp['success'] = 0
                resp['error'] = 'USER_EXISTS'
        elif local.data['action'] == 'login':
            try:
                username = local.data['username']
                password = local.data['password']
            except KeyError:
                logger.warning('Missing parameter')
                raise BadRequest()

            token = self.hashpw(password)

            user = self.get_user(username, token)
            if user is None:
                resp['success'] = 0
            else:
                resp['success'] = 1
                resp['token'] = token
                resp['access_level'] = user.access_level
        elif local.data['action'] == 'get':
            user = local.session.query(User)\
                .filter(User.username == local.data['username']).first()
            if user is None:
                raise NotFound()
            resp = self.get_user_info(user)
        elif local.data['action'] == 'list':
            query = local.session.query(User)\
                .filter(User.hidden == False)\
                .order_by(desc(User.score))\
                .order_by(desc(User.id))
            if 'institute' in local.data:
                query = query\
                    .filter(User.institute_id == local.data['institute'])
            users, resp['num'] = self.sliced_query(query)
            resp['users'] = map(self.get_user_info, users)
        elif local.data['action'] == 'update':
            if local.user is None:
                raise Unauthorized()
            resp['success'] = 0
            if 'institute' in local.data and \
               local.data['institute'] is not None:
                local.user.institute_id = int(local.data['institute'])
                resp['success'] = 1
            if 'email' in local.data and \
               local.data['email'] != '' and \
               local.user.email != local.data['email']:
                resp = self.check_email(local.data['email'])
                if not resp['success']:
                    return resp
                local.user.email = local.data['email']
                resp['success'] = 1
            if 'old_password' in local.data and \
               local.data['old_password'] != '':
                old_token = self.hashpw(local.data['old_password'])
                if local.user.password != old_token:
                    resp['error'] = 'user.edit.wrong'
                    return resp
                if len(local.data['password']) < 5:
                    resp['error'] = 'signup.errors.password'
                    return resp
                new_token = self.hashpw(local.data['password'])
                local.user.password = new_token
                resp['token'] = new_token
                resp['success'] = 1
            local.session.commit()
        else:
            raise BadRequest()
        return resp

    def task_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            query = local.session.query(Task)\
                .filter(Task.access_level >= local.access_level)\
                .order_by(desc(Task.id))
            if 'tag' in local.data:
                query = query.filter(Task.tags.any(name=local.data['tag']))
            tasks, resp['num'] = self.sliced_query(query)
            resp['tasks'] = []
            for t in tasks:
                task = dict()
                task['id'] = t.id
                task['name'] = t.name
                task['title'] = t.title
                resp['tasks'].append(task)
        elif local.data['action'] == 'get':
            t = local.session.query(Task)\
                .filter(Task.name == local.data['name'])\
                .filter(Task.access_level >= local.access_level).first()
            if t is None:
                raise NotFound()
            resp['id'] = t.id
            resp['name'] = t.name
            resp['title'] = t.title
            resp['statements'] =\
                dict([(l, s.digest) for l, s in t.statements.iteritems()])
            resp['submission_format'] =\
                [sfe.filename for sfe in t.submission_format]
            for i in ['time_limit', 'memory_limit', 'task_type']:
                resp[i] = getattr(t.active_dataset, i)
            att = []
            for (name, obj) in t.attachments.iteritems():
                att.append((name, obj.digest))
            resp['attachments'] = att
            resp['tags'] = [tag.name for tag in t.tags if tag.hidden is False]
        elif local.data['action'] == 'stats':
            t = local.session.query(Task)\
                .filter(Task.name == local.data['name'])\
                .filter(Task.access_level >= local.access_level).first()
            if t is None:
                raise NotFound()
            resp['nsubs'] = t.nsubs
            resp['nusers'] = t.nusers
            resp['nsubscorrect'] = t.nsubscorrect
            resp['nuserscorrect'] = t.nuserscorrect
            best = local.session.query(TaskScore)\
                .filter(TaskScore.task == t)\
                .filter(TaskScore.score == 100)\
                .order_by(TaskScore.time)\
                .slice(0, 10).all()
            resp['best'] = [{'username': b.user.username,
                             'time': b.time} for b in best]
        else:
            raise BadRequest()
        return resp

    def tag_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            tags = local.session.query(Tag)\
                .order_by(Tag.id)\
                .filter(Tag.hidden == False).all()
            resp['tags'] = [t.name for t in tags]
            return resp

        resp['success'] = 0
        if local.data['action'] == 'create':
            if local.access_level >= 4:
                raise Unauthorized()
            try:
                if len(local.data['description']) < 5:
                    resp['error'] = 'tags.description_short'
                else:
                    tag = Tag(name=local.data['tag'],
                              description=local.data['description'],
                              hidden=False)
                    local.session.add(tag)
                    local.session.commit()
                    resp['success'] = 1
            except IntegrityError:
                resp['error'] = 'tags.tag_exists'
        elif local.data['action'] == 'delete':
            if local.access_level >= 4:
                raise Unauthorized()
            tag = local.session.query(Tag)\
                .filter(Tag.name == local.data['tag']).first()
            if tag is None:
                resp['error'] = 'tags.tag_doesnt_exist'
            elif tag.hidden is True and local.access_level > 0:
                raise Unauthorized()
            else:
                local.session.delete(tag)
                local.session.commit()
                resp['success'] = 1
        elif local.data['action'] == 'add':
            if local.access_level >= 5:
                raise Unauthorized()
            tag = local.session.query(Tag)\
                .filter(Tag.name == local.data['tag']).first()
            task = local.session.query(Task)\
                .filter(Task.name == local.data['task']).first()
            if tag is None:
                resp['error'] = 'tags.tag_doesnt_exist'
            elif tag.hidden is True and local.access_level > 0:
                raise Unauthorized()
            elif task is None:
                resp['error'] = 'tags.task_doesnt_exist'
            elif tag in task.tags:
                resp['error'] = 'tags.task_has_tag'
            else:
                task.tags.append(tag)
                local.session.commit()
                resp['success'] = 1
        elif local.data['action'] == 'remove':
            if local.access_level >= 5:
                raise Unauthorized()
            tag = local.session.query(Tag)\
                .filter(Tag.name == local.data['tag']).first()
            task = local.session.query(Task)\
                .filter(Task.name == local.data['task']).first()
            if tag is None:
                resp['error'] = 'tags.tag_doesnt_exist'
            elif tag.hidden is True and local.access_level > 0:
                raise Unauthorized()
            elif task is None:
                resp['error'] = 'tags.tag_doesnt_exist'
            elif tag not in task.tags:
                resp['error'] = 'tags.task_hasnt_tag'
            else:
                task.tags.remove(tag)
                local.session.commit()
                resp['success'] = 1
        else:
            raise BadRequest()
        return resp

    def test_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            tests = local.session.query(Test)\
                .filter(Test.access_level >= local.access_level)\
                .order_by(Test.id).all()
            resp['tests'] = []
            for t in tests:
                test = {
                    'name': t.name,
                    'description': t.description,
                    'max_score': t.max_score
                }
                if local.user is not None:
                    testscore = local.session.query(TestScore)\
                        .filter(TestScore.test_id == t.id)\
                        .filter(TestScore.user_id == local.user.id).first()
                    if testscore is not None:
                        test['score'] = testscore.score
                resp['tests'].append(test)
        elif local.data['action'] == 'get':
            test = local.session.query(Test)\
                .filter(Test.name == local.data['test_name'])\
                .filter(Test.access_level >= local.access_level).first()
            if test is None:
                raise NotFound()
            resp['name'] = test.name
            resp['description'] = test.description
            resp['questions'] = []
            for i in test.questions:
                q = dict()
                q['type'] = i.type
                q['text'] = i.text
                q['max_score'] = i.score
                ansdata = json.loads(i.answers)
                if i.type == 'choice':
                    q['choices'] = [t[0] for t in ansdata]
                else:
                    q['answers'] = [[t[0], len(t[1])] for t in ansdata]
                resp['questions'].append(q)
        elif local.data['action'] == 'answer':
            test = local.session.query(Test)\
                .filter(Test.name == local.data['test_name'])\
                .filter(Test.access_level >= local.access_level).first()
            if test is None:
                raise NotFound()
            data = local.data['answers']
            for i in xrange(len(test.questions)):
                q = test.questions[i]
                ansdata = json.loads(q.answers)
                if q.type == 'choice':
                    resp[i] = [q.wrong_score, 'wrong']
                    try:
                        if data[i] is None:
                            resp[i] = [0, 'empty']
                        elif ansdata[int(data[i])][1]:
                            resp[i] = [q.score, 'correct']
                    except IndexError:
                        pass
                    continue
                else:
                    for key, correct in ansdata:
                        ans = data[i].get(key, None)
                        if len(ans) != len(correct):
                            resp[i] = [q.wrong_score, 'wrong']
                        for a in xrange(len(ans)):
                            if ans[a] is None:
                                resp[i] = [0, 'empty']
                                break
                            if q.type == 'number':
                                an = float(ans[a])
                                cor = float(correct[a])
                            else:
                                an = ans[a].lower()
                                cor = correct[a].lower()
                            if an != cor:
                                resp[i] = [q.wrong_score, 'wrong']
                    if resp.get(i, None) is None:
                        resp[i] = [q.score, 'correct']
            if local.user is not None:
                score = sum([q[0] for q in resp.itervalues()])
                testscore = local.session.query(TestScore)\
                    .filter(TestScore.test_id == test.id)\
                    .filter(TestScore.user_id == local.user.id).first()
                if testscore is None:
                    testscore = TestScore(score=score)
                    testscore.user = local.user
                    testscore.test = test
                    local.session.add(testscore)
                else:
                    if score > testscore.score:
                        testscore.score = score
                local.session.commit()
        else:
            raise BadRequest()
        return resp

    def submission_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            task = local.session.query(Task)\
                .filter(Task.name == local.data['task_name']).first()
            if task is None:
                raise NotFound()
            if local.user is None:
                raise Unauthorized()
            subs = local.session.query(Submission)\
                .filter(Submission.user_id == local.user.id)\
                .filter(Submission.task_id == task.id)\
                .order_by(desc(Submission.timestamp)).all()
            submissions = []
            for s in subs:
                submission = dict()
                submission['id'] = s.id
                submission['task_id'] = s.task_id
                submission['timestamp'] = make_timestamp(s.timestamp)
                submission['files'] = []
                for name, f in s.files.iteritems():
                    fi = dict()
                    if s.language is None:
                        fi['name'] = name
                    else:
                        fi['name'] = name.replace('%l', s.language)
                    fi['digest'] = f.digest
                    submission['files'].append(fi)
                result = s.get_result()
                for i in ['compilation_outcome', 'evaluation_outcome']:
                    submission[i] = getattr(result, i, None)
                if result is not None and result.score is not None:
                    submission['score'] = round(result.score, 2)
                submissions.append(submission)
            resp['submissions'] = submissions
        elif local.data['action'] == 'details':
            s = local.session.query(Submission)\
                .filter(Submission.id == local.data['id']).first()
            if s is None:
                raise NotFound()
            if local.user is None or s.user_id != local.user.id:
                raise Unauthorized()
            submission = dict()
            submission['id'] = s.id
            submission['task_id'] = s.task_id
            submission['timestamp'] = make_timestamp(s.timestamp)
            submission['language'] = s.language
            submission['files'] = []
            for name, f in s.files.iteritems():
                fi = dict()
                if s.language is None:
                    fi['name'] = name
                else:
                    fi['name'] = name.replace('%l', s.language)
                fi['digest'] = f.digest
                submission['files'].append(fi)
            result = s.get_result()
            for i in ['compilation_outcome', 'evaluation_outcome',
                      'compilation_stdout', 'compilation_stderr',
                      'compilation_time', 'compilation_memory']:
                submission[i] = getattr(result, i, None)
            if result is not None and result.score is not None:
                submission['score'] = round(result.score, 2)
            if result is not None and result.score_details is not None:
                tmp = json.loads(result.score_details)
                if len(tmp) > 0 and 'text' in tmp[0]:
                    subt = dict()
                    subt['testcases'] = tmp
                    subt['score'] = submission['score']
                    subt['max_score'] = 100
                    submission['score_details'] = [subt]
                else:
                    submission['score_details'] = tmp
                for subtask in submission['score_details']:
                    for testcase in subtask['testcases']:
                        data = json.loads(testcase['text'])
                        testcase['text'] = data[0] % tuple(data[1:])
            else:
                submission['score_details'] = None
            resp = submission
        elif local.data['action'] == 'new':
            if local.user is None:
                raise Unauthorized()
            lastsub = local.session.query(Submission)\
                .filter(Submission.user_id == local.user.id)\
                .order_by(desc(Submission.timestamp)).first()
            if lastsub is not None and \
               make_datetime() - lastsub.timestamp < timedelta(seconds=20):
                return {'success': 0, 'error': 'submission.short_interval'}

            # TODO: implement archives and (?) partial submissions
            try:
                task = local.session.query(Task)\
                    .filter(Task.name == local.data['task_name'])\
                    .filter(Task.access_level >= local.access_level).first()
            except KeyError:
                raise NotFound()
            resp = dict()
            resp['success'] = 1

            # Detect language
            files = []
            sub_lang = None
            for sfe in task.submission_format:
                f = local.data['files'].get(sfe.filename)
                if f is None:
                    return {'success': 0, 'error': 'submission.files_missing'}
                if len(f['data']) > config.max_submission_length:
                    return {'success': 0, 'error': 'submission.files_too_big'}
                f['name'] = sfe.filename
                files.append(f)
                if sfe.filename.endswith('.%l'):
                    language = None
                    for ext, l in SOURCE_EXT_TO_LANGUAGE_MAP.iteritems():
                        if f['filename'].endswith(ext):
                            language = l
                    if language is None:
                        return {'success': 0,
                                'error': 'submission.language_unknown'}
                    elif sub_lang is not None and sub_lang != language:
                        return {'success': 0,
                                'error': 'submission.language_different'}
                    else:
                        sub_lang = language

            # Add the submission
            timestamp = make_datetime()
            submission = Submission(timestamp,
                                    sub_lang,
                                    user=local.user,
                                    task=task)
            for f in files:
                digest = self.file_cacher.put_file_content(
                    f['data'].encode('utf-8'),
                    'Submission file %s sent by %s at %d.' % (
                        f['name'], local.user.username,
                        make_timestamp(timestamp)))
                local.session.add(File(f['name'],
                                       digest,
                                       submission=submission))
            local.session.add(submission)
            local.session.commit()

            # Notify ES
            self.evaluation_service.new_submission(
                submission_id=submission.id
            )

            # Answer with submission data
            resp['id'] = submission.id
            resp['task_id'] = submission.task_id
            resp['timestamp'] = make_timestamp(submission.timestamp)
            resp['compilation_outcome'] = None
            resp['evaluation_outcome'] = None
            resp['score'] = None
            resp['files'] = []
            for name, f in submission.files.iteritems():
                fi = dict()
                if submission.language is None:
                    fi['name'] = name
                else:
                    fi['name'] = name.replace('%l', submission.language)
                fi['digest'] = f.digest
                resp['files'].append(fi)
        else:
            raise BadRequest()
        return resp

    def forum_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            forums = local.session.query(Forum)\
                .filter(Forum.access_level >= local.access_level)\
                .order_by(Forum.id).all()
            resp['forums'] = []
            for f in forums:
                forum = dict()
                forum['id'] = f.id
                forum['description'] = f.description
                forum['title'] = f.title
                forum['topics'] = f.ntopic
                forum['posts'] = f.npost
                if len(f.topics) > 0:
                    forum['lastpost'] = {
                        'username':     f.topics[0].last_writer.username,
                        'timestamp':    make_timestamp(f.topics[0].timestamp),
                        'topic_title':  f.topics[0].title,
                        'topic_id':     f.topics[0].id,
                        'num':          f.topics[0].npost
                    }
                resp['forums'].append(forum)
        elif local.data['action'] == 'new':
            if local.access_level > 1:
                raise Unauthorized()
            if local.data['title'] is None or \
               len(local.data['title']) < 4:
                return {"success": 0, "error": "forum.title_short"}
            if local.data['description'] is None or \
               len(local.data['description']) < 4:
                return {"success": 0, "error": "forum.description_short"}
            forum = Forum(title=local.data['title'],
                          description=local.data['description'],
                          access_level=7,
                          ntopic=0,
                          npost=0)
            local.session.add(forum)
            local.session.commit()
            resp['success'] = 1
        else:
            raise BadRequest()
        return resp

    def topic_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            forum = local.session.query(Forum)\
                .filter(Forum.access_level >= local.access_level)\
                .filter(Forum.id == local.data['forum']).first()
            if forum is None:
                raise NotFound()
            noAnswer = 'noAnswer' in local.data and local.data['noAnswer']
            resp['title'] = forum.title
            resp['description'] = forum.description
            query = local.session.query(Topic)\
                .filter(Topic.forum_id == forum.id)\
                .order_by(desc(Topic.timestamp))
            topics, resp['num'] = self.sliced_query(query)
            resp['numUnanswered'] = local.session.query(Topic)\
                .filter(Topic.forum_id == forum.id)\
                .filter(Topic.answered == False).count()
            resp['topics'] = []
            for t in topics:
                if noAnswer and t.answered is True:
                    continue
                topic = dict()
                topic['id'] = t.id
                topic['status'] = t.status
                topic['title'] = t.title
                topic['timestamp'] = make_timestamp(t.creation_timestamp)
                topic['posts'] = t.npost
                topic['views'] = t.nview
                topic['author_username'] = t.author.username
                topic['lastpost'] = {
                    'username':  t.last_writer.username,
                    'timestamp': make_timestamp(t.timestamp)
                }
                resp['topics'].append(topic)
        elif local.data['action'] == 'new':
            if local.user is None:
                raise Unauthorized()
            forum = local.session.query(Forum)\
                .filter(Forum.access_level >= local.access_level)\
                .filter(Forum.id == local.data['forum']).first()
            if forum is None:
                raise NotFound()
            if local.data['title'] is None or len(local.data['title']) < 4:
                return {"success": 0, "error": "forum.title_short"}
            if local.data['text'] is None or len(local.data['text']) < 4:
                return {"success": 0, "error": "post.text_short"}
            topic = Topic(status='open',
                          title=local.data['title'],
                          timestamp=make_datetime(),
                          creation_timestamp=make_datetime(),
                          answered=False)
            topic.forum = forum
            topic.last_writer = local.user
            topic.author = local.user
            topic.npost = 1
            post = Post(text=local.data['text'],
                        timestamp=make_datetime())
            post.author = local.user
            post.topic = topic
            post.forum = forum
            local.session.add(topic)
            local.session.add(post)
            forum.ntopic = len(forum.topics)
            forum.npost = local.session.query(Post)\
                .filter(Post.forum_id == forum.id).count()
            local.session.commit()
            resp['success'] = 1
        else:
            raise BadRequest()
        return resp

    def post_handler(self):
        resp = dict()
        if local.data['action'] == 'list':
            topic = local.session.query(Topic)\
                .filter(Topic.id == local.data['topic']).first()
            if topic is None or topic.forum.access_level < local.access_level:
                raise NotFound()
            topic.nview += 1
            local.session.commit()
            query = local.session.query(Post)\
                .filter(Post.topic_id == topic.id)\
                .order_by(Post.timestamp)
            posts, resp['num'] = self.sliced_query(query)
            resp['title'] = topic.title
            resp['forumId'] = topic.forum.id
            resp['forumTitle'] = topic.forum.title
            resp['posts'] = []
            for p in posts:
                post = dict()
                post['id'] = p.id
                post['text'] = p.text
                post['timestamp'] = make_timestamp(p.timestamp)
                post['author'] = self.get_user_info(p.author)
                resp['posts'].append(post)
        elif local.data['action'] == 'new':
            if local.user is None:
                raise Unauthorized()
            topic = local.session.query(Topic)\
                .filter(Topic.id == local.data['topic']).first()
            if topic is None or topic.forum.access_level < local.access_level:
                raise NotFound()
            if local.data['text'] is None or len(local.data['text']) < 4:
                return {"success": 0, "error": "post.text_short"}
            post = Post(text=local.data['text'],
                        timestamp=make_datetime())
            post.author = local.user
            post.topic = topic
            post.forum = topic.forum
            topic.timestamp = post.timestamp
            topic.answered = True
            topic.last_writer = local.user
            local.session.add(post)
            topic.forum.npost = local.session.query(Post)\
                .filter(Post.forum_id == topic.forum.id).count()
            topic.npost = local.session.query(Post)\
                .filter(Post.topic_id == topic.id).count()
            local.session.commit()
            resp['success'] = 1
        elif local.data['action'] == 'delete':
            if local.user is None:
                raise Unauthorized()
            post = local.session.query(Post)\
                .filter(Post.id == local.data['id']).first()
            if post is None:
                raise NotFound()
            if post.author != local.user and local.user.access_level > 2:
                raise Unauthorized()
            forum = post.topic.forum
            if post.topic.posts[0] == post:
                local.session.delete(post.topic)
                resp['success'] = 2
            else:
                local.session.delete(post)
                post.topic.npost = local.session.query(Post)\
                    .filter(Post.topic_id == post.topic.id).count()
                resp['success'] = 1
            forum.npost = local.session.query(Post)\
                .filter(Post.forum_id == forum.id).count()
            forum.ntopic = local.session.query(Topic)\
                .filter(Topic.forum_id == forum.id).count()
            local.session.commit()
        elif local.data['action'] == 'edit':
            if local.user is None:
                raise Unauthorized()
            post = local.session.query(Post)\
                .filter(Post.id == local.data['id']).first()
            if post is None:
                raise NotFound()
            if post.author != local.user and local.user.access_level > 2:
                raise Unauthorized()
            if local.data['text'] is None or len(local.data['text']) < 4:
                return {"success": 0, "error": "post.text_short"}
            post.text = local.data['text']
            local.session.commit()
            resp['success'] = 1
        else:
            raise BadRequest()
        return resp

    def pm_handler(self):
        resp = dict()
        if local.data['action'] == 'list_sent':
            if local.user is None:
                raise Unauthorized()
            query = local.session.query(PrivateMessage)\
                .filter(PrivateMessage.sender_id == local.user.id)\
                .order_by(PrivateMessage.timestamp)
            pms, resp['num'] = self.sliced_query(query)
            resp['pms'] = []
            for p in pms:
                pm = dict()
                pm['title'] = p.title
                pm['receiver'] = self.get_user_info(p.receiver)
                pm['timestamp'] = make_timestamp(p.timestamp)
                resp['pms'].append(pm)
        elif local.data['action'] == 'list_received':
            if local.user is None:
                raise Unauthorized()
            query = local.session.query(PrivateMessage)\
                .filter(PrivateMessage.receiver_id == local.user.id)\
                .order_by(PrivateMessage.timestamp)
            pms, resp['num'] = self.sliced_query(query)
            resp['pms'] = []
            for p in pms:
                pm = dict()
                pm['title'] = p.title
                pm['sender'] = self.get_user_info(p.sender)
                pm['timestamp'] = make_timestamp(p.timestamp)
                pm['read'] = p.read
                resp['pms'].append(pm)
        elif local.data['action'] == 'get':
            pm = local.session.query(PrivateMessage)\
                .filter(PrivateMessage.id == local.data['id']).first()
            if pm is None:
                raise NotFound()
            if pm.receiver != local.user and pm.sender != local.user:
                raise Unauthorized()
            if pm.receiver == local.user:
                pm.read = True
                local.session.commit()
            resp['title'] = pm.title
            resp['sender'] = self.get_user_info(p.sender)
            resp['receiver'] = self.get_user_info(p.receiver)
            resp['timestamp'] = make_timestamp(p.timestamp)
            resp['text'] = p.text
        elif local.data['action'] == 'new':
            if local.user is None:
                raise Unauthorized()
            if local.user.id == local.data['receiver_id']:
                return {'success': 0, 'error': 'pm.self'}
            pm = PrivateMessage(text=local.data['text'],
                                title=local.data['title'],
                                timestamp=make_datetime())
            pm.sender_id = local.user.id
            pm.receiver_id = local.data['receiver_id']
            pm.read = False
            local.session.add(pm)
            local.session.commit()
        elif local.data['action'] == 'get_unread':
            if local.user is None:
                raise Unauthorized()
            resp['num'] = local.session.query(PrivateMessage)\
                .filter(PrivateMessage.receiver_id == local.user.id)\
                .filter(PrivateMessage.read == False).count()
        return resp


class PracticeWebServer(Service):
    '''Service that runs the web server for practice.

    '''
    def __init__(self, shard):
        initialize_logging('PracticeWebServer', shard)

        Service.__init__(self, shard=shard)

        self.address = config.contest_listen_address[shard]
        self.port = config.contest_listen_port[shard]
        self.file_cacher = FileCacher(self)
        self.evaluation_service = self.connect_to(
            ServiceCoord('EvaluationService', 0))

        handler = APIHandler(self)

        self.wsgi_app = SharedDataMiddleware(handler, {
            '/':        ('cms.web', 'practice'),
            '/assets':  ('cms.web', 'assets')
        })

    def run(self):
        server = Server((self.address, self.port), self.wsgi_app)
        gevent.spawn(server.serve_forever)
        Service.run(self)
