# PiTiVi , Non-linear video editor
#
#       tests/test_integration.py
#
# Copyright (c) 2008, Alessandro Decina <alessandro.decina@collabora.co.uk>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin St, Fifth Floor,
# Boston, MA 02110-1301, USA.

"""Test pitivi core objects at the API level, simulating the UI input for
QA scenarios """

from collections import deque

from unittest import TestCase
from pitivi.application import FullGuiPitivi
from pitivi.configure import _get_root_dir
from pitivi.utils.timeline import MoveContext, TrimStartContext,\
    TrimEndContext
from pitivi.utils.signal import Signallable
from pitivi.utils.misc import quote_uri
from pitivi.stream import AudioStream, VideoStream
from pitivi.ui.zoominterface import Zoomable
import pitivi.instance
import gobject
import gst
import os


class WatchDog(object):

    """A simple watchdog timer to aid developing integration tests. If
    keepAlive() is not called every <timeout> ms, then the watchdog timer will
    quit the specified mainloop."""

    def __init__(self, mainloop, timeout=10000):
        self.timeout = timeout
        self.mainloop = mainloop
        self.will_quit = True
        self.keep_going = True
        self.activated = False

    def start(self):
        self.will_quit = True
        self.keep_going = True
        gobject.timeout_add(self.timeout, self._timeoutcb)

    def suspend(self):
        self.keepAlive()
        self.keep_going = False

    def _timeoutcb(self):
        if self.will_quit:
            self.mainloop.quit()
            self.activated = True
            self.keep_going = False
        self.will_quit = True
        return self.keep_going

    def keepAlive(self):
        self.will_quit = False


class TestWatchdog(TestCase):

    def testWatchdog(self):
        self.ml = gobject.MainLoop()
        wd = WatchDog(self.ml, timeout=100)
        self.timeout_called = False
        wd.start()
        gobject.timeout_add(2000, self._timeoutCb)
        self.ml.run()
        self.assertFalse(self.timeout_called)
        self.assertTrue(wd.activated)

    def testKeepAlive(self):
        self.ml = gobject.MainLoop()
        wd = WatchDog(self.ml, timeout=2000)
        self.timeout_called = False
        wd.start()
        gobject.timeout_add(500, wd.keepAlive)
        gobject.timeout_add(2500, self._timeoutCb)
        self.ml.run()
        self.assertTrue(self.timeout_called)
        self.assertFalse(wd.activated)

    def testSuspend(self):
        self.ml = gobject.MainLoop()
        wd = WatchDog(self.ml, timeout=500)
        self.timeout_called = False
        wd.start()
        wd.suspend()
        gobject.timeout_add(2000, self._timeoutCb)
        self.ml.run()
        self.assertTrue(self.timeout_called)
        self.assertFalse(wd.activated)

    def _timeoutCb(self):
        self.ml.quit()
        self.timeout_called = True
        return False


class Configuration(object):

    def __init__(self):
        # A list of [name, uri, props].
        self.sources = []
        self._sources_by_name = {}
        self._bad_sources_names = set()

    def clone(self):
        ret = Configuration()
        for source in self.sources:
            if self._isSourceGood(source):
                name, uri, props = source
                ret.addSource(name, uri, dict(props))
            else:
                ret.addBadSource(*source)
        return ret

    def addSource(self, name, uri, props=None, error=False):
        if name in self._sources_by_name:
            raise Exception("Duplicate source: '%d' already defined" % name)
        source = [name, uri, props]
        self.sources.append(source)
        self._sources_by_name[name] = source

    def updateSource(self, name, uri=None, props=None):
        source = self._sources_by_name[name]
        if uri:
            source[1] = uri
        if props:
            source[2].update(props)

    def addBadSource(self, name, uri):
        self.addSource(name, uri)
        self._bad_sources_names.add(name)

    def getUris(self):
        return set((source[1] for source in self.sources))

    def getGoodUris(self):
        return set((source[1] for source in self.getGoodSources()))

    def getGoodSources(self):
        return (source for source in self.sources if self._isSourceGood(source))

    def _isSourceGood(self, source):
        return source[0] not in self._bad_sources_names

    def matches(self, instance_runner):
        for name, uri, props in self.getGoodSources():
            if not name in instance_runner.timeline_objects_by_name:
                raise Exception("Project missing source %s" % name)
            timelineObject = instance_runner.timeline_objects_by_name[name]
            if timelineObject.factory.uri != uri:
                raise Exception("%s has wrong factory type!" % name)
            for prop, value in props.iteritems():
                actual = getattr(timelineObject, prop)
                if not actual == value:
                    raise Exception("%s.%s: actual: %r, expected: %r" %
                            (name, prop, actual, value))

        names = set((source[0] for source in self.getGoodSources()))
        timelineObjects = set(instance_runner.timeline_objects_by_name.iterkeys())
        if names != timelineObjects:
            raise Exception("Project has extra sources: %r" % (timelineObjects -
                names))

    def __iter__(self):
        return self.getGoodSources()


class InstanceRunner(Signallable):

    class container(object):

        def __init__(self):
            pass

    __signals__ = {
        "sources-loaded": [],
        "timeline-configured": [],
    }

    def __init__(self, instance, project_name):
        self.instance = instance
        self.watchdog = WatchDog(instance.mainloop, timeout=10000)
        self.used_factory_uris = set()
        self.errors = set()
        self.project = None
        self.project_name = project_name
        # A pitivi.timeline.timeline.Timeline instance.
        self.timeline = None
        # A map from pitivi.timeline.track.Track to container.
        self.containers_by_track = {}
        # A map from custom name to pitivi.timeline.timeline.TimelineObject.
        self.timeline_objects_by_name = {}
        # The timeline configuration to be applied when
        # the "new-project-loaded" event is generated.
        self._pending_configuration = None
        # The list of audio pitivi.timeline.track.Track objects
        # added to the timeline.
        self.timeline_audio_tracks = []
        # The list of video pitivi.timeline.track.Track objects
        # added to the timeline.
        self.timeline_video_tracks = []
        self.instance_alive = True
        instance.connect("new-project-loaded", self._newProjectLoadedCb)

    def loadConfiguration(self, configuration):
        """Schedule applying a configuration when a new project is created.

        @type configuration Configuration
        """
        self._pending_configuration = configuration

    def _newProjectLoadedCb(self, instance, project):
        self.project = instance.current
        self.project.name = self.project_name
        self.timeline = self.project.timeline
        for track in self.timeline.tracks:
            self._trackAddedCb(self.timeline, track)
        self.project.sources.connect("source-added", self._sourceAdded)
        self.project.sources.connect("discovery-error", self._discoveryError)
        self.project.sources.connect("ready", self._readyCb)
        self.timeline.connect("track-added", self._trackAddedCb)

        if self._pending_configuration:
            self._loadSources(self._pending_configuration)

    def _sourceAdded(self, medialibrary, factory):
        self.used_factory_uris.add(factory.uri)

    def _discoveryError(self, medialibrary, uri, reason, unused):
        self.errors.add(uri)

    def _readyCb(self, soucelist):
        if self._pending_configuration:
            assert self.used_factory_uris == self._pending_configuration.getGoodUris()
            self._setupTimeline(self._pending_configuration)
        else:
            assert not self.used_factory_uris
        self.emit("sources-loaded")

    def _loadSources(self, configuration):
        for uri in configuration.getUris():
            self.project.sources.addUri(uri)

    def _trackAddedCb(self, timeline, track):
        """Handle the addition of a track to a timeline.

        @type timeline pitivi.timeline.timeline.Timeline
        @type track pitivi.timeline.track.Track
        """
        container = self.container()
        stream_type = type(track.stream)
        if stream_type is AudioStream:
            self.timeline_audio_tracks.append(container)
        elif stream_type is VideoStream:
            self.timeline_video_tracks.append(container)
        else:
            raise Exception("Unknown type of track stream: %s" % stream_type)
        self.containers_by_track[track] = container
        container.transitions = {}
        container.track_objects_by_name = {}
        track.connect("transition-added", self._transitionAddedCb, container)
        track.connect("transition-removed", self._transitionRemovedCb,
            container)

    def _transitionAddedCb(self, track, transition, container):
        container.transitions[(transition.a, transition.b)] = transition

    def _transitionRemovedCb(self, track, transition, container):
        del container.transitions[(transition.a, transition.b)]

    def _setupTimeline(self, configuration):
        for name, uri, props in configuration:
            if not props:
                continue
            factory = self.project.sources.getUri(uri)
            if not factory:
                raise Exception("Could not find '%s' in sourcelist" % name)

            timeline_object = self.timeline.addSourceFactory(factory)
            for prop, value in props.iteritems():
                setattr(timeline_object, prop, value)
            self.timeline_objects_by_name[name] = timeline_object
            for track_object in timeline_object.track_objects:
                container = self.containers_by_track[track_object.track]
                assert name not in container.track_objects_by_name
                container.track_objects_by_name[name] = track_object

        self.emit("timeline-configured")

    def run(self):
        """Run the main loop of the application."""
        self.watchdog.start()
        self.instance.projectManager.newBlankProject()
        # Set a common zoom ratio so that things like edge snapping values
        # are consistent.
        # This operation must be done after the creation of the project!
        Zoomable.setZoomLevel((3 * Zoomable.zoom_steps) / 4)
        self.instance.run()

    def shutDown(self):
        if not self.instance_alive:
            return
        self.instance_alive = False

        def application_shutdown():
            assert self.instance.shutdown()
            # Return False so we won't be called again.
            return False
        gobject.idle_add(application_shutdown)

        if self.project:
            self.project.setModificationState(False)


class Brush(Signallable):
    """Scrubs your timelines until they're squeaky clean."""

    __signals__ = {
        "scrub-step": ["time", "priority"],
        "scrub-done": [],
    }

    def __init__(self, runner):
        self.context = None
        self.runner = runner
        self._steps = deque()

    def addSteps(self, steps_count,
            min_time=0, max_time=2 * 60 * 60 * gst.SECOND,
            min_priority=0, max_priority=10):
        """Add a number of steps between the specified limits (low->high)."""
        self.addStep(min_time, min_priority)
        # We already added one step, so the deltas will have to be split into
        # (steps_count - 1) intervals, so the last step will be the max.
        time_delta = max_time - min_time
        priority_delta = max_priority - min_priority
        # Do we have more than one step?
        for count in xrange(1, steps_count):
            # The position in the timeline.
            position = min_time + time_delta * count / (steps_count - 1)
            # The priority identifies the channel on which the operation
            # will take place.
            priority = min_priority + priority_delta * count / (steps_count - 1)
            self.addStep(position, priority)

    def addStep(self, time, priority):
        self._steps.append((time, priority))

    def scrub(self, context):
        self.context = context
        priority = gobject.PRIORITY_DEFAULT_IDLE * 2
        gobject.idle_add(self._scrubTimeoutCb, priority=priority)

    def _scrubTimeoutCb(self):
        self.runner.watchdog.keepAlive()
        time, priority = self._steps.popleft()
        print "Scrubbing to position %s, priority %s" % (time, priority)
        self.context.editTo(time, priority)
        self.emit("scrub-step", time, priority)
        if not self._steps:
            self.context.finish()
            self.emit("scrub-done")
            # Return False so we won't be called again.
            return False
        else:
            return True


class Base(TestCase):
    """
    Creates and runs a FullGuiPitivi object, then starts the mainloop.
    Uses a WatchDog to ensure that test cases will eventually terminate with an
    assertion failure if runtime errors occur inside the mainloop."""

    @staticmethod
    def _getMediaFile(filename):
        file_path = os.path.join(_get_root_dir(), "tests", "samples", filename)
        return quote_uri("file://" + file_path)

    def run(self, result):
        self._result = result
        self._num_failures = len(result.failures)
        self._num_errors = len(result.errors)
        TestCase.run(self, result)

    def setUp(self):
        TestCase.setUp(self)
        self.ptv = FullGuiPitivi()
        self.assertEqual(self.ptv.current, None,
                "The application should not have a project yet!")
        self.assertEquals(pitivi.instance.PiTiVi, self.ptv,
                "The application instance was not set correctly!")
        self.runner = InstanceRunner(self.ptv, project_name=self.id())
        self.video_uri = Base._getMediaFile("video.mkv")
        self.audio_uri = Base._getMediaFile("audio.ogg")
        self.unexisting_uri = Base._getMediaFile("unexisting.avi")

    def tearDown(self):
        pitivi_instance_exists = bool(pitivi.instance.PiTiVi)
        pitivi.instance.PiTiVi = None
        del self.ptv

        # Reset the Zoomable class status, otherwise it keeps references
        # to instances which have been deleted, causing segfaults.
        # TODO: Refactor the Zoomable class so we don't have to do this.
        Zoomable._instances = []

        # Make sure the application is always shut down.
        if self.runner.instance_alive:
            self.runner.shutDown()

        self.assertFalse(self.runner.watchdog.activated,
                "The application stopped because of the watchdog!")
        del self.runner

        # make sure the instance has been unset
        if (self._num_errors == self._result.errors and
            self._num_failures == self._result.failures and
            pitivi_instance_exists):
            raise Exception("Instance was not unset")

        TestCase.tearDown(self)


class TestBasic(Base):

    def testWatchdog(self):
        self.runner.run()
        self.assertTrue(self.runner.watchdog.activated)
        self.runner.watchdog.activated = False

    def testBasic(self):

        def newProjectLoaded(pitivi, project):
            self.runner.shutDown()

        self.ptv.connect("new-project-loaded", newProjectLoaded)
        self.runner.run()

    def testImport(self):

        def sourcesLoaded(runner):
            self.runner.shutDown()

        config = Configuration()
        config.addSource("object1", self.video_uri)
        config.addSource("object2", self.audio_uri)
        config.addBadSource("object3", self.unexisting_uri)

        self.runner.connect("sources-loaded", sourcesLoaded)
        self.runner.loadConfiguration(config)
        self.runner.run()

        # Make sure the sources have not been added to the timeline.
        self.assertFalse(hasattr(self.runner, "object1"))
        self.assertFalse(hasattr(self.runner, "object2"))
        self.assertEqual(self.runner.used_factory_uris,
                         set((self.video_uri, self.audio_uri)))
        self.assertEqual(self.runner.errors, set((self.unexisting_uri,)))

    def testConfigureTimeline(self):

        config = Configuration()
        config.addSource(
            "object1",
            self.video_uri,
            {
                "start": 0,
                "duration": gst.SECOND,
                "media-start": gst.SECOND,
            })
        config.addSource(
            "object2",
            self.audio_uri,
            {
                "start": gst.SECOND,
                "duration": gst.SECOND,
            })

        def timelineConfigured(runner):
            config.matches(self.runner)
            self.runner.shutDown()

        self.runner.loadConfiguration(config)
        self.runner.connect("timeline-configured", timelineConfigured)
        self.runner.run()

        self.assertTrue(self.runner.timeline_objects_by_name['object1'])
        self.assertTrue(self.runner.timeline_objects_by_name['object2'])
        self.assertTrue(self.runner.timeline_video_tracks[0].track_objects_by_name["object1"])
        self.assertTrue(self.runner.timeline_audio_tracks[0].track_objects_by_name["object2"])

    def testMoveSources(self):
        initial = Configuration()
        initial.addSource(
            "object1",
            self.video_uri,
            {
                "start": 0,
                "duration": gst.SECOND,
                "media-start": gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object2",
            self.audio_uri,
            {
                "start": gst.SECOND,
                "duration": gst.SECOND,
                "priority": 1,
            })
        final = Configuration()
        final.addSource(
            "object1",
            self.video_uri,
            {
                "start": 10 * gst.SECOND,
            })
        final.addSource(
            "object2",
            self.audio_uri,
            {
                "start": 11 * gst.SECOND,
                "priority": 2,
            })

        def timelineConfigured(runner):
            context = MoveContext(self.runner.timeline,
                self.runner.timeline_video_tracks[0].track_objects_by_name["object1"],
                set((self.runner.timeline_audio_tracks[0].track_objects_by_name["object2"],)))
            brush.addSteps(10)
            brush.addStep(10 * gst.SECOND, 1)
            brush.scrub(context)

        def scrubDone(brush):
            final.matches(self.runner)
            self.runner.shutDown()

        self.runner.loadConfiguration(initial)
        self.runner.connect("timeline-configured", timelineConfigured)

        brush = Brush(self.runner)
        brush.connect("scrub-done", scrubDone)

        self.runner.run()

    def testRippleMoveSimple(self):

        initial = Configuration()
        initial.addSource('clip1', self.video_uri, {
            "duration": gst.SECOND,
            "start": gst.SECOND,
            "priority": 2})
        initial.addSource('clip2', self.video_uri, {
            "duration": gst.SECOND,
            "start": 2 * gst.SECOND,
            "priority": 5})
        final = Configuration()
        final.addSource('clip1', self.video_uri, {
            "duration": gst.SECOND,
            "start": 11 * gst.SECOND,
            "priority": 0})
        final.addSource('clip2', self.video_uri, {
            "duration": gst.SECOND,
            "start": 12 * gst.SECOND,
            "priority": 3})

        def timelineConfigured(runner):
            initial.matches(self.runner)
            context = MoveContext(self.runner.timeline,
                self.runner.timeline_video_tracks[0].track_objects_by_name["clip1"], set())
            context.setMode(context.RIPPLE)
            brush.addStep(11 * gst.SECOND, 0)
            brush.scrub(context)

        def scrubDone(brush):
            final.matches(self.runner)
            self.runner.shutDown()

        self.runner.connect("timeline-configured", timelineConfigured)
        brush = Brush(self.runner)
        brush.connect("scrub-done", scrubDone)

        self.runner.loadConfiguration(initial)
        self.runner.run()

    def testRippleTrimStartSimple(self):
        initial = Configuration()
        initial.addSource('clip1', self.video_uri,
            {
                "start": gst.SECOND,
                "duration": gst.SECOND,
            })
        initial.addSource('clip2', self.video_uri,
            {
                "start": 2 * gst.SECOND,
                "duration": gst.SECOND,
            })
        initial.addSource('clip3', self.video_uri,
            {
                "start": 5 * gst.SECOND,
                "duration": 10 * gst.SECOND,
            })

        final = Configuration()
        final.addSource('clip1', self.video_uri,
            {
                "start": 6 * gst.SECOND,
                "duration": gst.SECOND,
            })
        final.addSource('clip2', self.video_uri,
            {
                "start": 7 * gst.SECOND,
                "duration": gst.SECOND,
            })
        final.addSource('clip3', self.video_uri,
            {
                "start": 10 * gst.SECOND,
                "duration": 5 * gst.SECOND,
            })

        self.runner.loadConfiguration(initial)

        def timelineConfigured(runner):
            context = TrimStartContext(self.runner.timeline,
                self.runner.timeline_video_tracks[0].track_objects_by_name["clip3"], set())
            context.setMode(context.RIPPLE)
            brush.addSteps(10)
            brush.addStep(10 * gst.SECOND, 0)
            brush.scrub(context)
        self.runner.connect("timeline-configured", timelineConfigured)

        def scrubDone(brush):
            final.matches(self.runner)
            self.runner.shutDown()

        brush = Brush(self.runner)
        brush.connect("scrub-done", scrubDone)
        self.runner.run()


class TestSeeking(Base):

    def setUp(self):
        Base.setUp(self)
        # Positions in the timeline where to seek.
        self.positions = deque()

    def _startSeeking(self):
        # The index of the current step.
        self.seeks_count = 0
        # The number of "position" events generated by the pipeline.
        self.positions_count = 0
        self.runner.project.pipeline.connect("position", self._positionCb)
        gobject.idle_add(self._seekTimeoutCb)

    def _seekTimeoutCb(self):
        if self.positions:
            self.runner.watchdog.keepAlive()
            self.seeks_count += 1
            self.runner.project.pipeline.seek(self.positions[0])
            return True
        self.assertEqual(self.positions_count, self.seeks_count)
        self.runner.shutDown()
        return False

    def _positionCb(self, pipeline, position):
        self.positions_count += 1
        self.assertEqual(position, self.positions.popleft())

    def testSeeking(self):
        config = Configuration()
        clips_count = 10
        for i in xrange(0, clips_count):
            config.addSource("clip%d" % i, self.video_uri, {
                "start": i * gst.SECOND,
                "duration": gst.SECOND,
                "priority": i % 2,
            })
        self.runner.loadConfiguration(config)

        def timelineConfigured(runner):
            timeline_duration = self.runner.timeline.duration
            # Seek from position 0 to position timeline_duration.
            self.positions.extend(
                    [i * gst.SECOND
                     for i in xrange(timeline_duration / gst.SECOND)])
            self.positions.append(timeline_duration)
            self._startSeeking()
        self.runner.connect("timeline-configured", timelineConfigured)
        self.runner.run()

    def testSeekingToSamePosition(self):
        config = Configuration()
        clips_count = 2
        for i in xrange(0, clips_count):
            config.addSource("clip%d" % i, self.video_uri, {
                "start": i * gst.SECOND,
                "duration": gst.SECOND,
                "priority": i % 2,
            })
        self.runner.loadConfiguration(config)

        def timelineConfigured(runner):
            timeline_duration = self.runner.timeline.duration
            self.positions.append(0)
            self.positions.append(0)
            self.positions.append(timeline_duration / 2)
            self.positions.append(timeline_duration / 2)
            self.positions.append(timeline_duration)
            self.positions.append(timeline_duration)
            self._startSeeking()
        self.runner.connect("timeline-configured", timelineConfigured)
        self.runner.run()


class TestRippleExtensive(Base):
    """Test suite for ripple editing minutia and corner-cases"""

    def setUp(self):
        Base.setUp(self)
        # Create a sequence of adjacent clips in staggered formation, each one
        # second long.
        self.initial = Configuration()
        for i in xrange(0, 10):
            self.initial.addSource('clip%d' % i, self.video_uri,
                {'start': gst.SECOND * i, 'duration': gst.SECOND,
                    'priority': i % 2})
        # Create a list of 10 scenarios.
        self.finals = []
        for i in xrange(0, 10):
            # we're going to repeat the same operation using each clip as the
            # focus of the editing context. We create one final
            # configuration for the expected result of each scenario.
            final = Configuration()
            for j in xrange(0, 10):
                if j < i:
                    start = gst.SECOND * j
                    priority = j % 2
                else:
                    start = gst.SECOND * (j + 10)
                    priority = (j % 2) + 1
                props = {'start': start,
                         'duration': gst.SECOND,
                         'priority': priority}
                final.addSource('clip%d' % j, self.video_uri, props)
            self.finals.append(final)
        self.context = None
        self.brush = Brush(self.runner)
        self.runner.loadConfiguration(self.initial)
        self.runner.connect("timeline-configured", self.timelineConfigured)
        self.brush.connect("scrub-done", self.scenarioDoneCb)

    # when the timeline is configured, kick off the test by starting the
    # first scenario
    def timelineConfigured(self, runner):
        self.runScenario(0)

    def runScenario(self, scenario_index):
        self.current_scenario_index = scenario_index
        clipname = "clip%d" % self.current_scenario_index
        # Create the context using a single clip as focus and
        # not specifying any other clips.
        context = MoveContext(self.runner.timeline,
            self.runner.timeline_video_tracks[0].track_objects_by_name[clipname], set())
        context.snap(False)
        context.setMode(context.RIPPLE)
        self.context = context
        # this isn't a method, but an attribute that will be set by specific
        # test cases
        self.scrub_func(context,
                        (self.current_scenario_index + 10) * gst.SECOND,
                        (self.current_scenario_index % 2) + 1)

    # Handle the finish of a scrub operation.
    def scenarioDoneCb(self, brush):
        scenario_expected_config = self.finals[self.current_scenario_index]
        self.context.finish()
        try:
            scenario_expected_config.matches(self.runner)
        except Exception, e:
            raise Exception("Scenario failed: %s" % self.current_scenario_index, e)
        # Reset the timeline.
        restore = MoveContext(self.runner.timeline, self.context.focus, set())
        restore.setMode(restore.RIPPLE)
        restore.editTo(self.current_scenario_index * gst.SECOND,
                       self.current_scenario_index % 2)
        restore.finish()
        self.initial.matches(self.runner)
        if self.current_scenario_index + 1 < len(self.finals):
            # Kick off the next scenario.
            self.runScenario(self.current_scenario_index + 1)
        else:
            # We finished the last scenario. Shut down the application.
            self.runner.shutDown()

    def testRippleMoveComplex(self):
        # in this test we move directly to the given position
        def rippleMoveComplexScrubFunc(context, position, priority):
            self.brush.addStep(position, priority)
            self.brush.scrub(context)
        self.scrub_func = rippleMoveComplexScrubFunc
        self.runner.run()

    # FIXME: This test fails for unknown reasons.
    def testRippleMoveComplexMultiple(self):
        # Same as above test, but scrub multiple times.
        def rippleMoveComplexScrubFunc(context, position, priority):
            self.brush.addSteps(100)
            self.brush.addStep(position, priority)
            self.brush.scrub(context)
        self.scrub_func = rippleMoveComplexScrubFunc
        self.runner.run()


class TestTransitions(Base):

    def testSimple(self):
        initial = Configuration()
        initial.addSource(
            "object1",
            self.video_uri,
            {
                "start": 0,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object2",
            self.video_uri,
            {
                "start": 5 * gst.SECOND,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object3",
            self.video_uri,
            {
                "start": 10 * gst.SECOND,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })

        moves = [
            (9 * gst.SECOND, 0),
            (1 * gst.SECOND, 0),
        ]

        expected = [
            ("object2", "object3", 10 * gst.SECOND, 4 * gst.SECOND, 0),
            ("object1", "object2", 1 * gst.SECOND, 4 * gst.SECOND, 0),
        ]

        def timelineConfigured(runner):
            nextMove()

        def nextMove():
            if moves:
                self._cur_move = moves.pop(0)
                context = MoveContext(self.runner.timeline,
                    self.runner.timeline_video_tracks[0].track_objects_by_name["object2"],
                        set([self.runner.timeline_video_tracks[0].track_objects_by_name["object2"]]))
                brush.addSteps(10)
                time, priority = self._cur_move
                brush.addStep(time, priority)
                brush.scrub(context)
            else:
                self.runner.shutDown()

        def scrubDone(brush):
            a, b, start, duration, priority = expected.pop(0)
            a = self.runner.timeline_video_tracks[0].track_objects_by_name[a]
            b = self.runner.timeline_video_tracks[0].track_objects_by_name[b]

            tr = self.runner.timeline_video_tracks[0].transitions[(a, b)]

            self.assertEqual(b.start, start)
            self.assertEqual(a.start + a.duration - start, duration)
            self.assertEqual(tr.start, start)
            self.assertEqual(tr.duration, duration)
            self.assertEqual(tr.priority, 0)
            self.assertEqual(a.priority, 0)
            self.assertEqual(b.priority, 0)
            nextMove()

        self.runner.loadConfiguration(initial)
        self.runner.connect("timeline-configured", timelineConfigured)

        brush = Brush(self.runner)
        brush.connect("scrub-done", scrubDone)

        self.runner.run()

    def testNoTransitionWhenMovingMultipleClips(self):
        initial = Configuration()
        initial.addSource(
            "object1",
            self.video_uri,
            {
                "start": 0,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object2",
            self.video_uri,
            {
                "start": 5 * gst.SECOND,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object3",
            self.video_uri,
            {
                "start": 10 * gst.SECOND,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })

        moves = [
            ("object1", 9 * gst.SECOND, 0),
            ("object3", 1 * gst.SECOND, 0),
        ]

        def timelineConfigured(runner):
            nextMove()

        def nextMove():
            if moves:
                self._cur_move = moves.pop(0)
                other, start, priority = self._cur_move
                context = MoveContext(self.runner.timeline,
                        self.runner.timeline_video_tracks[0].track_objects_by_name["object2"],
                        set([self.runner.timeline_video_tracks[0].track_objects_by_name[other]]))
                brush.addSteps(10)
                brush.addStep(start, priority)
                brush.scrub(context)
            else:
                self.runner.shutDown()

        def scrubDone(brush):
            self.assertEqual(self.runner.timeline_video_tracks[0].transitions, {})
            initial.matches(self.runner)
            nextMove()

        self.runner.loadConfiguration(initial)
        self.runner.connect("timeline-configured", timelineConfigured)

        brush = Brush(self.runner)
        brush.connect("scrub-done", scrubDone)

        self.runner.run()

    def testOverlapOnlyWithValidTransitions(self):
        initial = Configuration()
        initial.addSource(
            "object1",
            self.video_uri,
            {
                "start": 0,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object2",
            self.video_uri,
            {
                "start": 5 * gst.SECOND,
                "duration": 3 * gst.SECOND,
                "priority": 0,
            })
        initial.addSource(
            "object3",
            self.video_uri,
            {
                "start": 8 * gst.SECOND,
                "duration": 5 * gst.SECOND,
                "priority": 0,
            })

        phase2 = initial.clone()
        phase2.updateSource(
            "object2",
            props={
                "start": 4 * gst.SECOND,
            })

        phase3 = phase2.clone()
        phase3.updateSource(
            "object3",
            props={
                "duration": 1 * gst.SECOND,
            })

        phase4 = initial.clone()
        phase4.updateSource(
            "object2",
            props={
                "start": 3 * gst.SECOND,
            })
        phase4.updateSource(
            "object3",
            props={
                "start": 5 * gst.SECOND,
                "duration": 5 * gst.SECOND,
            })

        moves = [
            # [1------]    [3--[2==]]
            (MoveContext, "object2", 9 * gst.SECOND, 0, initial, []),

            # [1--[2=]]    [3-------]
            (MoveContext, "object2", 1 * gst.SECOND, 0, initial, []),

            # [1------]    [3-------]
            #        [2--]
            (MoveContext, "object2", 4 * gst.SECOND, 0, phase2,
                [("object1", "object2")]),

            # Activates overlap prevention
            # [1------]
            #      [3-------]
            #        [2--]

            (MoveContext, "object3", 3 * gst.SECOND, 0, phase2,
                [("object1", "object2")]),

            # [1------]  [3-]
            #        [2--]
            (TrimEndContext, "object3", 9 * gst.SECOND, 0, phase3,
                [("object1", "object2")]),

            # Activates overlap prevention
            # [1------]
            #        [3-]
            #        [2--]
            (MoveContext, "object3", 4 * gst.SECOND, 0, phase3,
                [("object1", "object2")]),

            # Activates overlap prevention
            # [1------]
            #       [3]
            #        [2--]
            (MoveContext, "object3", long(3.5 * gst.SECOND), 0, phase3,
                [("object1", "object2")]),

            # Activates overlap prevention
            # [1      ]
            #         [3]
            #        [2  ]
            (MoveContext, "object3", long(4.5 * gst.SECOND), 0,
                phase3, [("object1", "object2")]),

            # Next few commands build this arrangement
            # [1      ]
            #     [2    ]
            #          [3   ]

            (MoveContext, "object2", 3 * gst.SECOND, 0,
                None, None),
            (MoveContext, "object3", 5 * gst.SECOND, 0,
                None, None),
            (TrimEndContext, "object3", 10 * gst.SECOND, 0,
                phase4, [("object1", "object2"), ("object2",
                    "object3")]),

            # Activates Overlap Prevention
            # [1      ]
            #     [2    ]
            #       [3   ]

            (MoveContext, "object3", 4 * gst.SECOND, 0,
                phase4, [("object1", "object2"),
                    ("object2", "object3")]),

        ]

        nmoves = len(moves)

        def timelineConfigured(runner):
            nextMove()

        def nextMove():
            if moves:
                print "cur_move: %d/%d" % (nmoves - len(moves) + 1, nmoves)
                self._cur_move = moves.pop(0)
                context, focus, start, priority, config, trans = self._cur_move
                obj = self.runner.timeline_video_tracks[0].track_objects_by_name[focus]
                context = context(self.runner.timeline, obj, set())
                brush.addSteps(10)
                brush.addStep(start, priority)
                brush.scrub(context)
            else:
                self.runner.shutDown()

        def scrubDone(brush):
            connect, focus, stream, priority, config, trans = self._cur_move

            if config:
                config.matches(self.runner)

            if trans:
                expected = set([(self.runner.timeline_video_tracks[0].track_objects_by_name[a],
                                 self.runner.timeline_video_tracks[0].track_objects_by_name[b])
                                for a, b in trans])

                self.assertEqual(set(self.runner.timeline_video_tracks[0].transitions.keys()),
                    expected)
            nextMove()

        self.runner.loadConfiguration(initial)
        self.runner.connect("timeline-configured", timelineConfigured)

        brush = Brush(self.runner)
        brush.connect("scrub-done", scrubDone)

        self.runner.run()

    def testSaveAndLoadWithTransitions(self):
        pass
