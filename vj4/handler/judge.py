import asyncio
import calendar
import datetime
import logging
import time
from bson import objectid

from vj4 import app
from vj4 import constant
from vj4 import job
from vj4.model import builtin
from vj4.model import domain
from vj4.model import opcount
from vj4.model import queue
from vj4.model import record
from vj4.model.adaptor import contest
from vj4.model.adaptor import problem
from vj4.service import bus
from vj4.handler import base


_logger = logging.getLogger(__name__)


async def _post_judge(rdoc):
  await opcount.force_inc(**opcount.OPS['run_code'], ident=opcount.PREFIX_USER + str(rdoc['uid']),
                          operations=rdoc['time_ms'])
  accept = rdoc['status'] == constant.record.STATUS_ACCEPTED
  post_coros = [bus.publish('record_change', rdoc['_id'])]
  # TODO(twd2): ignore no effect statuses like system error, ...
  if rdoc['type'] == constant.record.TYPE_SUBMISSION:
    if accept:
      # TODO(twd2): send ac mail
      pass
    if rdoc['tid']:
      post_coros.append(contest.update_status(rdoc['domain_id'], rdoc['tid'], rdoc['uid'],
                                              rdoc['_id'], rdoc['pid'], accept, rdoc['score']))
    if not rdoc.get('rejudged'):
      if await problem.update_status(rdoc['domain_id'], rdoc['pid'], rdoc['uid'],
                                     rdoc['_id'], rdoc['status']):
        post_coros.append(problem.inc(rdoc['domain_id'], rdoc['pid'], 'num_accept', 1))
        post_coros.append(domain.inc_user(rdoc['domain_id'], rdoc['uid'], num_accept=1))
      if accept:
        # TODO(twd2): enqueue rdoc['pid'] to recalculate rp.
        pass
    else:
      # TODO(twd2): enqueue rdoc['pid'] to recalculate rp.
      await job.record.user_in_problem(rdoc['uid'], rdoc['domain_id'], rdoc['pid'])
  await asyncio.gather(*post_coros)


@app.route('/judge/playground', 'judge_playground')
class JudgePlaygroundHandler(base.Handler):
  @base.require_priv(builtin.PRIV_READ_RECORD_CODE | builtin.PRIV_WRITE_RECORD
                     | builtin.PRIV_READ_PROBLEM_DATA | builtin.PRIV_READ_PRETEST_DATA)
  async def get(self):
    self.render('judge_playground.html')


@app.route('/judge/noop', 'judge_noop')
class JudgeNoopHandler(base.Handler):
  @base.require_priv(builtin.JUDGE_PRIV)
  async def get(self):
    self.json({})


@app.route('/judge/datalist', 'judge_datalist')
class JudgeDataListHandler(base.Handler):
  @base.get_argument
  @base.sanitize
  async def get(self, last: int=0):
    # TODO(iceboy): This function looks strange.
    # Judge will have PRIV_READ_PROBLEM_DATA,
    # domain administrator will have PERM_READ_PROBLEM_DATA.
    if not self.has_priv(builtin.PRIV_READ_PROBLEM_DATA):
      self.check_perm(builtin.PERM_READ_PROBLEM_DATA)
    pids = await problem.get_data_list(last)
    datalist = []
    for domain_id, pid in pids:
      datalist.append({'domain_id': domain_id, 'pid': pid})
    self.json({'list': datalist,
               'time': calendar.timegm(datetime.datetime.utcnow().utctimetuple())})


@app.route('/judge/{rid}/score', 'judge_score')
class RecordRejudgeHandler(base.Handler):
  @base.route_argument
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, rid: objectid.ObjectId, score: int, message: str=''):
    rdoc = await record.get(rid)
    if rdoc['domain_id'] == self.domain_id:
      self.check_perm(builtin.PERM_REJUDGE)
    else:
      self.check_priv(builtin.PRIV_REJUDGE)
    await record.rejudge(rdoc['_id'], False)
    await record.begin_judge(rid, self.user['_id'], self.user['_id'],
                             constant.record.STATUS_FETCHED)
    update = {'$set': {}, '$push': {}}
    update['$set']['status'] = constant.record.STATUS_ACCEPTED if score == 100 \
                               else constant.record.STATUS_WRONG_ANSWER
    update['$push']['cases'] = {
      'status': update['$set']['status'],
      'score': score,
      'time_ms': 0,
      'memory_kb': 0,
      'judge_text': message,
    }
    await record.next_judge(rid, self.user['_id'], self.user['_id'], **update)
    rdoc = await record.end_judge(rid, self.user['_id'], self.user['_id'],
                                  update['$set']['status'], score, 0, 0)
    await _post_judge(rdoc)
    self.json_or_redirect(self.referer_or_main)


@app.connection_route('/judge/consume-conn', 'judge_consume-conn')
class JudgeNotifyConnection(base.Connection):
  @base.require_priv(builtin.PRIV_READ_RECORD_CODE | builtin.PRIV_WRITE_RECORD)
  async def on_open(self):
    self.rids = {}  # delivery_tag -> rid
    self.channel = await queue.consume('judge', self._on_queue_message)
    asyncio.ensure_future(self.channel.close_event.wait()).add_done_callback(lambda _: self.close())

  async def _on_queue_message(self, tag, *, rid):
    # This callback runs in the receiver loop of the amqp connection. Should not block here.
    async def start():
      # TODO(iceboy): Error handling?
      rdoc = await record.begin_judge(rid, self.user['_id'], self.id,
                                      constant.record.STATUS_FETCHED)
      if rdoc:
        used_time = await opcount.get(**opcount.OPS['run_code'],
                                      ident=opcount.PREFIX_USER + str(rdoc['uid']))
        if used_time >= opcount.OPS['run_code']['max_operations']:
          await asyncio.gather(
              record.end_judge(rid, self.user['_id'], self.id,
                               constant.record.STATUS_CANCELED, 0, 0, 0),
              self.channel.basic_client_ack(tag))
          await bus.publish('record_change', rid)
          return
        self.rids[tag] = rdoc['_id']
        self.send(rid=str(rdoc['_id']), tag=tag, pid=str(rdoc['pid']), domain_id=rdoc['domain_id'],
                  lang=rdoc['lang'], code=rdoc['code'], type=rdoc['type'])
        await bus.publish('record_change', rdoc['_id'])
      else:
        # Record not found, eat it.
        await self.channel.basic_client_ack(tag)

    asyncio.get_event_loop().create_task(start())

  async def on_message(self, *, key, tag, **kwargs):
    if key == 'next':
      rid = self.rids[tag]
      update = {}
      if 'status' in kwargs:
        update.setdefault('$set', {})['status'] = int(kwargs['status'])
      if 'compiler_text' in kwargs:
        update.setdefault('$push', {})['compiler_texts'] = str(kwargs['compiler_text'])
      if 'judge_text' in kwargs:
        update.setdefault('$push', {})['judge_texts'] = str(kwargs['judge_text'])
      if 'case' in kwargs:
        update.setdefault('$push', {})['cases'] = {
          'status': int(kwargs['case']['status']),
          'score': int(kwargs['case']['score']),
          'time_ms': int(kwargs['case']['time_ms']),
          'memory_kb': int(kwargs['case']['memory_kb']),
          'judge_text': str(kwargs['case']['judge_text']),
        }
      if 'progress' in kwargs:
        update.setdefault('$set', {})['progress'] = float(kwargs['progress'])
      await record.next_judge(rid, self.user['_id'], self.id, **update)
      await bus.publish('record_change', rid)
    elif key == 'end':
      rid = self.rids.pop(tag)
      rdoc, _ = await asyncio.gather(record.end_judge(rid, self.user['_id'], self.id,
                                                      int(kwargs['status']),
                                                      int(kwargs['score']),
                                                      int(kwargs['time_ms']),
                                                      int(kwargs['memory_kb'])),
                                     self.channel.basic_client_ack(tag))
      await _post_judge(rdoc)
    elif key == 'nack':
      await self.channel.basic_client_nack(tag)

  async def on_close(self):
    async def close():
      await asyncio.gather(*[record.end_judge(rid, self.user['_id'], self.id,
                                              constant.record.STATUS_CANCELED, 0, 0, 0)
                             for rid in self.rids.values()])
      await asyncio.gather(*[bus.publish('record_change', rid)
                             for rid in self.rids.values()])
      # There is a bug in current version's aioamqp and we cannot use no_wait=True here.
      await self.channel.close()

    asyncio.get_event_loop().create_task(close())
