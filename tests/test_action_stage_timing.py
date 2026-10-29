import unittest
from unittest.mock import MagicMock, patch

import vk_batch as engine
import vk_community_ui as ui
import vk_action_timing as timing
import tests.test_scheduled_action as scheduled_fixtures
import vk_scheduled_action as scheduled


class StageTimingTests(unittest.TestCase):
    def test_samples_are_buffered_and_flushed_once_without_private_values(self):
        ticks=iter([10,10.123,20,20.050])
        timer=timing.StageTimings(monotonic=lambda:next(ticks))
        observer=MagicMock()
        with timer.measure('click'):pass
        original=RuntimeError('private exception body')
        with self.assertRaises(RuntimeError) as caught:
            with timer.measure('confirmation'):raise original
        self.assertIs(caught.exception,original)
        observer.assert_not_called()
        with patch.object(timing.diagnostics,'emit') as emit:
            timer.flush(observer);timer.flush(observer)
        self.assertEqual(emit.call_count,2)
        self.assertEqual(emit.call_args_list[0].kwargs,dict(timing_stage='click',duration_ms=123,timing_outcome='complete'))
        self.assertEqual(emit.call_args_list[1].kwargs,dict(timing_stage='confirmation',duration_ms=50,timing_outcome='failed'))
        self.assertNotIn('private',repr(timer.samples))

    def test_clock_and_emitter_failures_do_not_replace_action_exception(self):
        timer=timing.StageTimings(monotonic=lambda:(_ for _ in ()).throw(RuntimeError('clock')))
        original=ValueError('action')
        with self.assertRaises(ValueError) as caught:
            with timer.measure('click'):raise original
        self.assertIs(caught.exception,original)
        self.assertEqual(timer.samples,())
        timer=timing.StageTimings(monotonic=iter([1,2]).__next__)
        with timer.measure('click'):pass
        with patch.object(timing.diagnostics,'emit',side_effect=RuntimeError('write')):
            timer.flush(MagicMock())

    def test_summary_excludes_failed_samples_and_leaves_unmeasured_unknown(self):
        samples=[dict(timing_stage='click',duration_ms=n,timing_outcome='complete') for n in (10,20,30,40)]
        samples.append(dict(timing_stage='click',duration_ms=9000,timing_outcome='failed'))
        result=timing.summarize(samples)
        self.assertEqual(result['click'],dict(samples=4,failed_samples=1,median_ms=25,p95_ms=40))
        self.assertEqual(result['cooldown'],dict(samples=0,failed_samples=0,median_ms=None,p95_ms=None))
        self.assertEqual(len(result),7)

    def test_click_like_measures_only_outward_click_after_last_deadline(self):
        elapsed=[0.0];events=[]
        timer=timing.StageTimings(monotonic=lambda:elapsed[0])
        button=MagicMock();page=MagicMock()
        def resolve(*args):elapsed[0]+=40;return button
        def click(**kwargs):events.append('click');elapsed[0]+=.125
        def deadline():events.append('deadline');elapsed[0]+=5;return 321
        button.click.side_effect=click
        page.wait_for_timeout.side_effect=lambda ms:elapsed.__setitem__(0,elapsed[0]+ms/1000)
        with patch.object(engine,'post_footer_button',side_effect=resolve),patch.object(engine,'ensure_ok'):
            engine.click_like(page,10,20,before_click=deadline,timings=timer)
        self.assertEqual(events,['deadline','click'])
        self.assertEqual(timer.samples[0]['duration_ms'],125)
        self.assertEqual(timer.samples[0]['timing_stage'],'click')

    def test_community_click_excludes_trial_checks_reservation_and_deadline(self):
        elapsed=[0.0];timer=timing.StageTimings(monotonic=lambda:elapsed[0])
        page=MagicMock();final=MagicMock();proof={'community_id':99}
        ui._PREPARED[page]=(dict(proof),MagicMock(),final)
        def click(**kwargs):elapsed[0]+=9 if kwargs.get('trial') else .25
        def check(*args):elapsed[0]+=7;return final
        def reserve():elapsed[0]+=3
        def deadline():elapsed[0]+=2;return 123
        final.click.side_effect=click
        with patch.object(ui,'_check_destination',side_effect=check),patch.object(engine,'ensure_ok'):
            ui.submit(page,proof,before_publish=reserve,before_final_click=deadline,timings=timer)
        self.assertEqual(timer.samples,(dict(timing_stage='click',duration_ms=250,timing_outcome='complete'),))

    def test_personal_click_excludes_dialog_preparation_and_reservation(self):
        elapsed=[0.0];timer=timing.StageTimings(monotonic=lambda:elapsed[0])
        page=MagicMock();opener=MagicMock();final=MagicMock()
        state=dict(ok=True,state={'wall':True,'direct_message':False})
        def evaluate(script,*args):
            elapsed[0]+=2
            if script==engine.PUBLISH_CONTROLS_JS:return dict(ok=True,candidates=[dict(disabled=False)])
            return state
        page.evaluate.side_effect=evaluate;page.locator.return_value=final
        opener.click.side_effect=lambda **kwargs:elapsed.__setitem__(0,elapsed[0]+8)
        final.click.side_effect=lambda **kwargs:elapsed.__setitem__(0,elapsed[0]+.25)
        def reserve():elapsed[0]+=4
        with patch.object(engine,'post_footer_button',return_value=opener),\
             patch.object(engine,'wait_share_destination',return_value=state),patch.object(engine,'ensure_ok'):
            engine.click_share_then_publish(page,10,20,before_publish=reserve,timings=timer)
        self.assertEqual(timer.samples,(dict(timing_stage='click',duration_ms=250,timing_outcome='complete'),))

    def test_clock_reversal_is_unknown_and_failed_only_summary_has_no_duration(self):
        timer=timing.StageTimings(monotonic=iter([2,1,3,4]).__next__)
        with timer.measure('click'):pass
        with self.assertRaises(RuntimeError):
            with timer.measure('confirmation'):raise RuntimeError('original')
        summary=timing.summarize(timer.samples)
        self.assertEqual(summary['click']['samples'],0)
        self.assertIsNone(summary['click']['median_ms'])
        self.assertEqual(summary['confirmation'],dict(samples=0,failed_samples=1,median_ms=None,p95_ms=None))


class ScheduledTimingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=scheduled_fixtures.ScheduledActionTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)

    def test_samples_emit_only_after_cleanup_and_preserve_verified_result(self):
        fixture=self.fixture;events=[];samples=[]
        def process(page,db,gid,row,pause,*,safety,**kwargs):
            safety.reserve(row['post_id'],'like')
            with safety.stage_timings.measure('click'):events.append('click')
            engine.status_write(db,gid,row['post_id'],'like','verified','offline')
        original=timing.diagnostics.emit
        def emit(callback,event,**data):
            if event=='stage_timing':
                self.assertEqual(events[-2:],[('sleep',20),'closed'])
                samples.append(data)
                raise RuntimeError('telemetry failure')
            return original(callback,event,**data)
        with fixture.patches(scheduled),patch.object(engine,'process_one',side_effect=process),\
             patch('time.sleep',side_effect=lambda n:events.append(('sleep',n))),\
             patch.object(timing.diagnostics,'emit',side_effect=emit):
            fixture.context.close.side_effect=lambda:events.append('closed')
            result=fixture.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(result['status'],'verified')
        stages={sample['timing_stage'] for sample in samples}
        self.assertTrue({'browser_open','source_navigation','source_guard','click','cooldown'}<=stages)
        self.assertNotIn('community_prepare',stages)

    def test_failed_community_prepare_keeps_original_type_and_never_cools_unreserved(self):
        fixture=self.fixture;samples=[]
        original=timing.diagnostics.emit
        def emit(callback,event,**data):
            if event=='stage_timing':samples.append(data)
            else:return original(callback,event,**data)
        with fixture.patches(scheduled),patch.object(ui,'scan_destination',return_value=[]),\
             patch.object(ui,'prepare',side_effect=ui.CommunityPreparationTimeout()),\
             patch('time.sleep') as sleep,patch.object(timing.diagnostics,'emit',side_effect=emit):
            with self.assertRaises(ui.CommunityPreparationTimeout):
                fixture.execute(scheduled,1,'community',before_attempt=lambda:self.fail('reserved'),record_attempt=lambda _:None)
        sleep.assert_not_called()
        self.assertEqual([s['timing_outcome'] for s in samples if s['timing_stage']=='community_prepare'],['failed'])
        self.assertNotIn('click',[s['timing_stage'] for s in samples])

    def test_failed_click_keeps_original_exception_and_cools_once_before_telemetry(self):
        fixture=self.fixture;events=[];samples=[];original=RuntimeError('outward transport')
        def process(page,db,gid,row,pause,*,safety,**kwargs):
            safety.reserve(row['post_id'],'like')
            with safety.stage_timings.measure('click'):raise original
        real_emit=timing.diagnostics.emit
        def emit(callback,event,**data):
            if event=='stage_timing':
                self.assertEqual(events,[20,'closed'])
                samples.append(data)
                raise ValueError('diagnostic failure')
            return real_emit(callback,event,**data)
        with fixture.patches(scheduled),patch.object(engine,'process_one',side_effect=process),\
             patch('time.sleep',side_effect=events.append),patch.object(timing.diagnostics,'emit',side_effect=emit):
            fixture.context.close.side_effect=lambda:events.append('closed')
            with self.assertRaises(RuntimeError) as caught:
                fixture.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertIs(caught.exception,original)
        self.assertEqual([s['timing_outcome'] for s in samples if s['timing_stage']=='click'],['failed'])
        self.assertEqual([s['timing_outcome'] for s in samples if s['timing_stage']=='cooldown'],['complete'])


if __name__=='__main__':unittest.main()
