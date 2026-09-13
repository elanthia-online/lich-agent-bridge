require 'minitest/autorun'
require 'timeout'
require 'tmpdir'
require 'fileutils'
require 'open3'

ENV['LAB_BRIDGE_LIBRARY_ONLY'] = '1'
ENV['LAB_CONTROLLER_MANIFEST'] = File.expand_path('fixtures/controllers.json', __dir__)

module Script
  def self.current = (@current ||= Object.new)
  def self.running = []
  def self.hidden = []
end

module XMLData
  def self.name = 'Testmage'
  def self.game = 'GSIV'
  def self.player_id = '12345'
  def self.room_title = '[Town Square Central]'
  def self.health = 139
  def self.max_health = 139
  def self.mana = 285
  def self.max_mana = 285
  def self.spirit = 10
  def self.max_spirit = 10
  def self.stamina = 103
  def self.max_stamina = 103
  def self.stance_text = 'guarded'
  def self.mind_value = 0
  def self.encumbrance_value = 0
  def self.roundtime_end = 0
  def self.server_time_offset = 0
  def self.indicator = { 'IconSTUNNED' => 'n', 'IconDEAD' => 'n', 'IconSTANDING' => 'y' }
  def self.injuries = { 'chest' => { 'wound' => 1, 'scar' => 0 } }
end

FakeLabObject = Struct.new(:id, :noun, :name, :full_name, :status)
FakeActiveSpell = Struct.new(:num, :timeleft)

module GameObj
  class << self
    attr_accessor :inventory, :container_map, :right, :left, :room_npcs
  end

  def self.inv = inventory || []
  def self.containers = container_map || {}
  def self.right_hand = right
  def self.left_hand = left
  def self.npcs = room_npcs || []
end

module Map
  Room = Struct.new(:id)
  def self.current = Room.new(1000)
end

module Spell
  def self.active = []
end

module LabInventory
  class << self
    attr_accessor :capture_calls, :scroll_capture_calls
  end

  def self.begin_capture(command, target)
    self.capture_calls ||= []
    self.capture_calls << [command, target]
  end

  def self.begin_scroll_operation_capture(command)
    self.scroll_capture_calls ||= []
    self.scroll_capture_calls << command
  end
end

def hide_me = nil
def respond(message) = ($plain_responses ||= []) << message
def _respond(message) = ($raw_responses ||= []) << message
def strip_xml(value, type: nil) = value
def waitrt? = (($roundtime_waits ||= []) << :roundtime)
def waitcastrt? = (($roundtime_waits ||= []) << :cast_roundtime)

CONTROLLER_REGISTRY_SOURCE = File.realpath(File.expand_path('../lich/lab-controller-registry.rb', __dir__))
module LabControllerRegistry
  class ManifestError < StandardError; end

  class Registry
    def self.load(_path)
      raise ManifestError, 'controllers[6].safe_handoff has unsupported field(s): return_seconds'
    end
  end
end
$LOADED_FEATURES << CONTROLLER_REGISTRY_SOURCE unless $LOADED_FEATURES.include?(CONTROLLER_REGISTRY_SOURCE)
load File.expand_path('../lich/lab-bridge.lic', __dir__)
$LOADED_FEATURES.delete(CONTROLLER_REGISTRY_SOURCE)

class LabBridgeTest < Minitest::Test
  def test_combat_report_uses_raw_trials_and_separate_verified_handoff
    trials = [{ routine: 'a', target_id: '123' }]
    runtime = Object.new
    runtime.define_singleton_method(:status) { { objective: { results: trials } } }
    capture = Object.new
    calls = []
    capture.define_singleton_method(:report) do |trials:, safe:|
      calls << [trials, safe]
      LabCombatReport.unavailable('fixture')
    end
    result = { ok: false, code: 'work_failed', details: { recovery_complete: true } }
    LichAgentBridge.attach_combat_report({ runtime: runtime, combat_capture: capture }, result)
    assert_equal [[trials, true]], calls
    refute result[:ok]
    assert_equal 'work_failed', result[:code]
    assert_equal 'fixture', JSON.parse(result[:details][:combat_report_json])['reason']
  end

  def test_reporting_failure_does_not_relabel_success_or_handoff
    capture = Object.new
    capture.define_singleton_method(:report) { |**_| raise 'fixture' }
    result = { ok: true, code: 'completed', details: { recovery_complete: true } }
    LichAgentBridge.attach_combat_report({ combat_capture: capture }, result)
    assert result[:ok]
    assert result[:details][:recovery_complete]
    assert_equal 'report_failed', JSON.parse(result[:details][:combat_report_json])['reason']
  end

  def test_runtime_load_replaces_a_cached_controller_registry
    assert_equal CONTROLLER_REGISTRY_SOURCE, LabControllerRegistry::Registry.method(:load).source_location.first
  end

  class OwnedTestScript
    attr_accessor :thread, :exit_error, :cleanup_blocked
    attr_reader :file_name, :vars, :kills
    def initialize(path, args)
      @file_name, @vars, @kills = path, [args, *args.split(' ')], 0
    end
    def join(timeout) = !cleanup_blocked && thread&.join(timeout) && self
    def kill_sync(timeout:)
      @kills += 1
      thread&.kill
      join(timeout)
    end
    def completed_successfully? = exit_error.nil? && @kills.zero?
  end

  def with_pinned_test_runtime
    Dir.mktmpdir('lab-bridge-pilot-') do |temporary|
      scripts = File.join(temporary, 'scripts')
      Dir.mkdir(scripts)
      Dir.mkdir(File.join(temporary, 'data'))
      %w[lab-test-runner.rb lab-test-runner.lic].each do |name|
        FileUtils.cp(File.expand_path("../lich/#{name}", __dir__), File.join(scripts, name))
      end
      File.write(File.join(scripts, 'probe.lic'), "raise 'wrong fixed args' unless Script.current.vars.drop(1) == ['normal']\n")
      suite = {'version' => 1, 'id' => 'probe', 'script' => 'probe', 'files' => ['probe.lic'],
               'cases' => [{'id' => 'normal', 'args' => ['normal'], 'assertions' => [{'field' => 'room_id', 'op' => 'unchanged'}]}],
               'limits' => {'case_seconds' => 0.2, 'run_seconds' => 1, 'cleanup_seconds' => 0.1}}
      File.write(File.join(scripts, 'suite.json'), JSON.generate(suite))
      pins = %w[suite.json probe.lic lab-test-runner.lic lab-test-runner.rb].to_h { |name| [name, Digest::SHA256.file(File.join(scripts, name)).hexdigest] }
      registration = {'name' => 'test-probe', 'script' => 'lab-test-runner', 'summary' => 'Synthetic test.',
                      'characters' => ['Testmage'], 'result_global' => '$lab_test_result', 'signal_global' => '$lab_test_cancel',
                      'lanes' => %w[movement combat], 'owner_scripts' => %w[lab-test-runner probe],
                      'safe_handoff' => {'kind' => 'room', 'room_id' => '1000'}, 'capability_action' => 'start',
                      'test_suite' => {'manifest' => 'suite.json', 'files' => pins},
                      'actions' => [{'name' => 'start', 'kind' => 'launch', 'launch_mode' => 'start',
                                     'command_template' => 'lab-test probe {revision} {case_id}',
                                     'script_args_template' => 'probe {revision} {case_id}',
                                     'policy' => {'category' => 'configuration', 'confirmation_required' => true},
                                     'parameters' => [{'name' => 'revision', 'type' => 'enum', 'values' => [pins['suite.json']]},
                                                      {'name' => 'case_id', 'type' => 'enum', 'values' => %w[normal all]}]}]}
      controller = LabControllerRegistry::Controller.new(registration, 'test')
      registry = LabControllerRegistry::Registry.new([controller], 'synthetic')
      command = "lab-test probe #{pins['suite.json']} normal"
      match = registry.match(command)
      action = {action_id: 'owned-test-action', character: 'Testmage', generation: LichAgentBridge.session_generation,
                expected_room_id: '1000', command: command, expires_at: Time.now.to_f + 5}
      originals = %i[start_child current running? __find_script_file].to_h { |name| [name, (Script.method(name) rescue nil)] }
      bridge_originals = %i[test_runtime_supported? snapshot_state publish_snapshot publish_controller_result request action_request].to_h { |name| [name, LichAgentBridge.method(name)] }
      old_dir = $script_dir
      old_run = LichAgentBridge.instance_variable_get(:@test_run)
      $script_dir = scripts
      LichAgentBridge.instance_variable_set(:@test_run, nil)
      events, instances = Queue.new, []
      Script.define_singleton_method(:current) { Thread.current[:pilot_script] || originals[:current].call }
      Script.define_singleton_method(:running?) { |name| instances.any? { |instance| File.basename(instance.file_name, '.lic') == name && !instance.join(0) } }
      Script.define_singleton_method(:__find_script_file) { |name| "#{name}.lic" }
      Script.define_singleton_method(:start_child) do |name, args, _options|
        instance = OwnedTestScript.new(File.join(scripts, "#{name}.lic"), args)
        instances << instance
        instance.thread = Thread.new do
          Thread.current[:pilot_script] = instance
          begin
            load instance.file_name
          rescue StandardError => error
            instance.exit_error = error
          end
        end
        instance
      end
      LichAgentBridge.define_singleton_method(:test_runtime_supported?) { true }
      LichAgentBridge.define_singleton_method(:snapshot_state) do
        {character: 'Testmage', generation: action[:generation], room: {id: '1000'}, dead: false, stunned: false,
         hands: {right: nil, left: nil}, scripts: [], owners: {movement: nil, combat: nil}}
      end
      LichAgentBridge.define_singleton_method(:publish_snapshot) { |**_options| events << [:snapshot] }
      LichAgentBridge.define_singleton_method(:publish_controller_result) { |_controller, id, result| events << [:result, id, result] }
      LichAgentBridge.define_singleton_method(:request) { |*_args, **_options| action.merge(status: 'dispatched', stop_requested: false) }
      requests = @requests
      LichAgentBridge.define_singleton_method(:action_request) { |path, payload| requests << [path, payload]; {} }
      yield action, match, events, instances
    ensure
      instances&.each { |instance| instance.kill_sync(timeout: 0.1) unless instance.join(0) }
      originals&.each do |name, method|
        method ? Script.define_singleton_method(name, method) : Script.singleton_class.send(:remove_method, name)
      end
      bridge_originals&.each { |name, method| LichAgentBridge.define_singleton_method(name, method) }
      $script_dir = old_dir
      LichAgentBridge.instance_variable_set(:@test_run, old_run)
    end
  end

  def test_pinned_bridge_launch_claim_and_monitor_publish_owned_report_after_exit
    with_pinned_test_runtime do |action, match, events, instances|
      LichAgentBridge.execute_controller_action(action, match)
      assert_equal [:snapshot], Timeout.timeout(2) { events.pop }
      event = Timeout.timeout(2) { events.pop }
      assert_equal [:result, action[:action_id]], event.first(2)
      assert event.last[:ok], event.last.inspect
      assert_equal %w[lab-test-runner.lic probe.lic], instances.map { |instance| File.basename(instance.file_name) }
      assert instances.all? { |instance| instance.join(0) }
      report = JSON.parse(File.read(event.last[:details][:report_path]))
      assert_equal action[:action_id], report['action_id']
      assert_equal 'passed', report['cases'][0]['status']
      assert LichAgentBridge.test_run_released?(LichAgentBridge.instance_variable_get(:@test_run))
    end
  end

  def test_pinned_bridge_rejects_unsupported_runtime_without_launch
    with_pinned_test_runtime do |action, match, _events, instances|
      LichAgentBridge.define_singleton_method(:test_runtime_supported?) { false }
      LichAgentBridge.execute_controller_action(action, match)
      assert_empty instances
      assert_equal 'failed', @requests.last[1][:outcome]
      assert_includes @requests.last[1][:detail], 'unsupported loaded Lich lifecycle'
    end
  end

  def test_incomplete_cleanup_result_arrives_even_when_wrapper_teardown_is_blocked
    with_pinned_test_runtime do |action, match, events, instances|
      original_start = Script.method(:start_child)
      Script.define_singleton_method(:start_child) do |*args|
        instance = original_start.call(*args)
        instance.cleanup_blocked = true
        instance
      end
      LichAgentBridge.execute_controller_action(action, match)
      assert_equal [:snapshot], Timeout.timeout(2) { events.pop }
      event = Timeout.timeout(2) { events.pop }
      refute event.last[:ok]
      refute event.last[:details][:cleanup_complete]
      refute LichAgentBridge.test_run_released?(LichAgentBridge.instance_variable_get(:@test_run))
      assert_equal 2, instances.length
      LichAgentBridge.execute_controller_action(action, match)
      assert_equal 2, instances.length, 'an unresolved cleanup must not launch a successor'
      assert_equal 'failed', @requests.last[1][:outcome]
      instances.each { |instance| instance.cleanup_blocked = false }
      assert instances.all? { |instance| instance.join(0.2) }
    end
  end

  def test_private_report_failure_starts_no_target_and_does_not_orphan_wrapper
    with_pinned_test_runtime do |action, match, events, instances|
      report_directory = File.expand_path('../data/lab-script-tests', $script_dir)
      File.write(report_directory, 'protected existing file')
      LichAgentBridge.execute_controller_action(action, match)
      assert_equal [:snapshot], Timeout.timeout(2) { events.pop }
      result = Timeout.timeout(2) { events.pop }.last
      refute result[:ok]
      assert_equal 'runner_failed', result[:code]
      assert_equal ['lab-test-runner.lic'], instances.map { |instance| File.basename(instance.file_name) }
      assert LichAgentBridge.test_run_released?(LichAgentBridge.instance_variable_get(:@test_run))
      assert_equal 'protected existing file', File.read(report_directory)
    end
  end

  def test_failed_wrapper_lifecycle_cannot_publish_a_passed_child_report_as_success
    with_pinned_test_runtime do |action, match, events, _instances|
      original_start = Script.method(:start_child)
      Script.define_singleton_method(:start_child) do |*args|
        instance = original_start.call(*args)
        instance.define_singleton_method(:completed_successfully?) { false } if args.first == 'lab-test-runner'
        instance
      end
      LichAgentBridge.execute_controller_action(action, match)
      assert_equal [:snapshot], Timeout.timeout(2) { events.pop }
      result = Timeout.timeout(2) { events.pop }.last
      refute result[:ok]
      assert_equal 'wrapper_failed', result[:code]
      refute result[:details][:assertions_passed]
    end
  end

  def test_test_exclusion_retains_an_owned_child_after_wrapper_exit
    child = Object.new
    finished = false
    child.define_singleton_method(:join) { |_timeout| finished ? self : nil }
    runner = Object.new
    runner.define_singleton_method(:cleanup_complete?) { !!child.join(0) }
    wrapper = Object.new
    wrapper.define_singleton_method(:join) { |_timeout| self }
    run = {instance: wrapper, runner: runner}
    refute LichAgentBridge.test_run_released?(run)
    finished = true
    assert LichAgentBridge.test_run_released?(run)
  end

  def test_test_snapshot_preserves_known_empty_hands_and_rejects_new_combat_owner
    original_snapshot = LichAgentBridge.method(:snapshot_state)
    state = {character: 'Testmage', generation: 'g', room: {id: '1000'},
             dead: false, stunned: false, hands: {right: nil, left: nil},
             scripts: ['lab-test-runner'], owners: {movement: 'lab-test-runner', combat: 'lab-test-runner'}}
    LichAgentBridge.define_singleton_method(:snapshot_state) { state }
    context = {'excluded_scripts' => [], 'owned_scripts' => ['lab-test-runner', 'probe']}
    snapshot = LichAgentBridge.test_local_snapshot(context)
    assert_equal '', snapshot['right_hand_id']
    assert_equal '', snapshot['left_hand_id']
    state[:owners][:combat] = 'bigshot'
    assert_raises(LabTestRunner::Halt) { LichAgentBridge.test_local_snapshot(context) }
  ensure
    LichAgentBridge.define_singleton_method(:snapshot_state, original_snapshot)
  end

  def test_test_control_checks_exact_run_identity_stop_and_bridge_kill_switch
    original_request = LichAgentBridge.method(:request)
    context = {'action_id' => 'owned-action', 'character' => 'Testmage',
               'generation' => LichAgentBridge.session_generation,
               'room_id' => '1000', 'command' => 'lab-test probe revision normal'}
    reply = context.transform_keys(&:to_sym).merge(expected_room_id: '1000', status: 'dispatched', stop_requested: false)
    LichAgentBridge.define_singleton_method(:request) { |*_args, **_options| reply }
    assert LichAgentBridge.test_run_control(context)
    reply[:action_id] = 'successor-action'
    refute LichAgentBridge.test_run_control(context)
    reply[:action_id] = 'owned-action'
    reply[:stop_requested] = true
    refute LichAgentBridge.test_run_control(context)
    reply[:stop_requested] = false
    LichAgentBridge.instance_variable_set(:@actions_enabled, false)
    refute LichAgentBridge.test_run_control(context)
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request)
  end

  class SyntheticQuickChild
    attr_accessor :alive, :cleanup_blocked, :quick_combat_runtime, :quick_combat_result,
                  :controller_runtime, :controller_result
    attr_reader :name, :kills
    def initialize(name)
      @name, @alive, @cleanup_blocked, @kills = name, true, false, []
    end
    def running? = @alive
    def stopping? = !@kills.empty?
    def kill(async:)
      @kills << async
      @alive = false unless @cleanup_blocked
    end
    def join(_timeout) = (!@alive && !@cleanup_blocked ? self : nil)
    def completed_successfully? = !@alive
    def exit_error = nil
  end

  class SyntheticQuickRuntime
    attr_reader :requests
    attr_accessor :current_status, :auto_stop
    def initialize(child)
      @child, @requests, @auto_stop = child, [], true
      @current_status = { mode: 'trial', state: :running, reason: nil }
    end
    def status
      terminal = %w[completed stopped].include?(@current_status[:state].to_s)
      { refuge: { room_id: 1000, phase: terminal ? 'finished' : 'working', returned: terminal,
                  equipment_restored: terminal }, work_result: terminal ? @current_status.dup : nil }.merge(@current_status).freeze
    end
    attr_reader :execution_window
    def activate_supervised(valid:)
      return false if @activated || !@execution_window || valid.call != true
      @activated = true
    end
    def bind_execution_window(**window)
      return false if @execution_window
      @execution_window = window
      true
    end
    def request(action, valid: nil)
      @requests << [action, valid, Thread.current]
      if action == 'stop' && !valid && @auto_stop
        @current_status = { state: :stopped, reason: 'manual_stop' }
        @child.alive = false
      end
      { accepted: true, status: status }
    end
  end

  def synthetic_large_quick_status(command: Array.new(12, 'force unarmed grapple until 3'), count: 100)
    { mode: 'trial', state: :completed, reason: 'sequence_dispatched', room_id: 123456,
      room_epoch: 98765, target_id: '123456789', actions: 110, sends: 110, sends_unverified: 0,
      configuration: { preset: 'synthetic', profile: 'synthetic-area', sequence: 'synthetic-probe' },
      limits: { scope: :run, max_actions: 200, max_seconds: 1000.0, max_ineffective: 3 },
      timing: { clock: :monotonic, started_at: 12345678.123456, updated_at: 12345679.123456,
                ended_at: 12345679.123456, deadline: 12346678.123456, target_started_at: 12345678.123456 },
      observations_total: 110, observations_dropped: 10, observation_sequence_range: { first: 11, last: 110 },
      retreat_pending: false, escape_sends: 0, control_error: 'synthetic prior rejection',
      observations: Array.new(count) { |index| { sequence: index + 11, target_id: '123456789', room_id: 123456,
        room_epoch: 98765, command: command, outcome: :sent, sends: 1, reserved_actions: 1,
        started_at: 12345678.123456, at: 12345679.123456, evidence_reason: :no_evidence } } }
  end

  def test_quick_status_bounds_transcript_without_losing_summary_or_newest_outcomes
    source = synthetic_large_quick_status
    original = JSON.generate(source)
    assert_operator original.bytesize, :>, 32_768
    compact = LichAgentBridge.quick_runtime_status(terminal_status: source)
    assert_operator JSON.generate(compact).bytesize, :<=, 32_768
    assert_equal original, JSON.generate(source), 'serialization must not mutate the runtime snapshot'
    expected_summary = JSON.parse(original, symbolize_names: true).reject { |key, _| key == :observations }
    assert_equal expected_summary, compact.reject { |key, _| %i[observations observation_transport].include?(key) }
    assert_operator compact[:observations].length, :>, 0
    assert_equal 110, compact[:observations].last[:sequence]
    assert_equal 1, compact[:observations].last[:sends]
    assert_equal 'sent', compact[:observations].last[:outcome]
    assert_equal 'json', compact[:observations].last[:command_presentation]
    assert_equal source[:observations].last[:command], JSON.parse(compact[:observations].last[:command])
    assert_equal 100 - compact[:observations].length, compact[:observation_transport][:omitted_entries]
    assert_equal({ first: compact[:observations].first[:sequence], last: 110 }, compact[:observation_transport][:retained_sequence_range])

    child = SyntheticQuickChild.new('bigshot')
    child.alive = false
    result = LichAgentBridge.controlled_terminal_result({ instance: child, action: { action_id: '0123456789abcdef' }, terminal_status: source }, true)
    assert_equal 'quick_sequence_dispatched', result[:code]
    assert_equal '0123456789abcdef', result[:details][:run_id]
    assert_equal false, result[:details][:effects_verified]
  end

  def test_quick_command_presentation_bounds_long_text_and_passes_real_event_validation
    status = synthetic_large_quick_status(command: Array.new(300, 'attack target'), count: 1)
    compact = LichAgentBridge.quick_runtime_status(terminal_status: status)
    event = compact[:observations].first
    assert_equal 4000, event[:command].length
    assert_equal JSON.generate(status[:observations].first[:command]).length - 4000, event[:command_omitted_characters]
    assert_equal 11, event[:sequence]
    assert_equal 0, compact[:observation_transport][:omitted_entries]
    payload = { character: 'Testmage', generation: 'synthetic-generation', observed_at: '2026-09-09T00:00:00Z',
                kind: 'controller_result', summary: 'Synthetic result', data: { details: { runtime: compact } } }
    root = File.expand_path('..', __dir__)
    output, error, result = Open3.capture3({ 'PYTHONPATH' => File.join(root, 'src') }, 'python3', '-c',
      'import json,sys; from lich_agent_bridge.protocol import MeaningfulEvent; MeaningfulEvent.from_mapping(json.load(sys.stdin)); print("validated")', stdin_data: JSON.generate(payload))
    assert result.success?, "Actual event validator rejected compact result: #{output} #{error}"
    assert_equal "validated\n", output
  end

  def test_quick_status_keeps_small_plain_reports_unchanged_and_rejects_oversized_summary
    source = { state: :running, observations: [{ sequence: 1, command: 'attack target', sends: 1 }] }
    compact = LichAgentBridge.quick_runtime_status(terminal_status: source)
    assert_equal JSON.parse(JSON.generate(source), symbolize_names: true), compact
    assert_equal 'attack target', compact[:observations].first[:command]
    refute compact[:observations].first.key?(:command_presentation)
    long_text = synthetic_large_quick_status(command: 'a' * 5000, count: 1)
    long_entry = LichAgentBridge.quick_runtime_status(terminal_status: long_text)[:observations].first
    assert_equal 4000, long_entry[:command].length
    assert_equal 1000, long_entry[:command_omitted_characters]
    assert_raises(LabControllerControls::Invalid) do
      LichAgentBridge.quick_runtime_status(terminal_status: { state: :running, control_error: 'x' * 32_769, observations: [] })
    end
  end

  def test_refuge_work_result_preserves_summary_without_duplicating_oversized_transcript
    source = synthetic_large_quick_status
    source[:work_result] = JSON.parse(JSON.generate(source), symbolize_names: true)
    source[:refuge] = { room_id: 1000, phase: 'finished', returned: true, equipment_restored: true }
    original = JSON.generate(source)
    compact = LichAgentBridge.quick_status_payload(source)
    assert_equal original, JSON.generate(source)
    assert_equal 'completed', compact[:work_result][:state]
    assert_equal 'sequence_dispatched', compact[:work_result][:reason]
    assert_equal 110, compact[:work_result][:observations_total]
    refute compact[:work_result].key?(:observations)
    assert_equal 'runtime.observations', compact[:work_result][:observation_transport][:observations_source]
    assert_equal 100, compact[:work_result][:observation_transport][:omitted_entries]
    assert_operator JSON.generate(compact).bytesize, :<=, 32_768
    payload = { character: 'Testmage', generation: 'synthetic-generation', observed_at: '2026-09-09T00:00:00Z',
                kind: 'controller_result', summary: 'Synthetic refuge result', data: { details: { runtime: compact } } }
    root = File.expand_path('..', __dir__)
    output, error, result = Open3.capture3({ 'PYTHONPATH' => File.join(root, 'src') }, 'python3', '-c',
      'import json,sys; from lich_agent_bridge.protocol import MeaningfulEvent; MeaningfulEvent.from_mapping(json.load(sys.stdin)); print("validated")', stdin_data: JSON.generate(payload))
    assert result.success?, "Actual event validator rejected refuge result: #{output} #{error}"
  end

  def test_refuge_trial_results_are_projected_within_the_event_depth_contract
    source = synthetic_large_quick_status(command: 'incant 702', count: 2)
    source[:observations] = [{ sequence: 1, type: 'engine_started', at: 1.0,
                              data: { 'behaviors' => '["survival", "engage"]' } }]
    source[:reason] = :completed
    source[:work_result] = { state: :completed, reason: :objective_complete, observations: [] }
    source[:refuge] = { room_id: 26_109, phase: 'finished', returned: true, equipment_restored: true }
    source[:objective] = {
      state: 'complete', planned: ['e'], current: nil, failure: nil, active: nil,
      results: [{ index: 1, routine: 'e', target_id: '154405112',
                  creature: { id: '154405112', name: 'a wraith', noun: 'wraith', type: 'undead', status: 'dead' },
                  actions: [{ at: 1.0, command: 'incant 702', status: 'resolved', reason: nil,
                              line: 'The wraith is struck.', spent: { mana: 4 } }],
                  samples: [{ at: 1.0, resources: { mana: 293 }, creature: { id: '154405112' }, state: { status: 'dead' } }],
                  outcome: 'killed', elapsed_seconds: 1.25 }]
    }

    compact = LichAgentBridge.quick_status_payload(source)
    assert_equal 1, compact[:objective][:results_count]
    refute compact[:objective].key?(:results)
    assert_equal [{ index: 1, routine: 'e', target_id: '154405112', creature: 'a wraith',
                    outcome: 'killed', actions: 1, samples: 1, elapsed_seconds: 1.25 }], compact[:trial_results]
    assert_equal '{"behaviors":"[\\"survival\\", \\"engage\\"]"}', compact[:observations].first[:data]
    assert_equal 'json', compact[:observations].first[:data_presentation]
    payload = { character: 'Testmage', generation: 'synthetic-generation', observed_at: '2026-09-09T00:00:00Z',
                kind: 'controller_result', summary: 'Synthetic trial result', data: { details: { runtime: compact } } }
    root = File.expand_path('..', __dir__)
    output, error, result = Open3.capture3({ 'PYTHONPATH' => File.join(root, 'src') }, 'python3', '-c',
      'import json,sys; from lich_agent_bridge.protocol import MeaningfulEvent; MeaningfulEvent.from_mapping(json.load(sys.stdin)); print("validated")', stdin_data: JSON.generate(payload))
    assert result.success?, "Actual event validator rejected trial result: #{output} #{error}"
  end

  def with_controlled_quick(inventory: false, area: false, legacy: nil, uncontrolled: false, script_args: nil)
    originals = {}
    replace = lambda do |object, name, &implementation|
      originals[[object, name]] = object.respond_to?(name) ? object.method(name) : nil
      object.define_singleton_method(name, &implementation)
    end
    raw = JSON.parse(File.read(File.expand_path('fixtures/controller-controls.json', __dir__)))
    raw['controllers'].first['lanes'] << 'inventory' if inventory
    raw['controllers'].first['script'] = 'bigshot'
    raw['controllers'].first['safe_handoff'] = { 'kind' => 'quick_refuge', 'room_id' => 1000, 'return_seconds' => 10 }
    raw['controllers'].first['control_owner_scripts'] = ['bigshot']
    raw['controllers'].first['actions'].first['script_args_template'] = "quick #{area ? 'watch' : 'trial'} --area profile"
    raw['controllers'].first['safe_handoff'] = legacy if legacy
    raw['controllers'].first['actions'].first['script_args_template'] = script_args if script_args
    if uncontrolled
      raw['controllers'].first.delete('control_owner_scripts')
      raw['controllers'].first['actions'].reject! { |action| action['kind'] == 'control' }
    end
    controllers = raw['controllers'].each_with_index.map { |item, index| LabControllerRegistry::Controller.new(item, "controllers[#{index}]") }
    registry = LabControllerRegistry::Registry.new(controllers, 'synthetic-controlled-fixture')
    old_registry, old_patterns = LichAgentBridge::CONTROLLER_REGISTRY, LichAgentBridge::SAFE_ACTIONS
    old_runs = LichAgentBridge.instance_variable_get(:@controlled_runs)
    old_generation = LichAgentBridge.session_generation
    LichAgentBridge.send(:remove_const, :CONTROLLER_REGISTRY)
    LichAgentBridge.const_set(:CONTROLLER_REGISTRY, registry)
    LichAgentBridge.send(:remove_const, :SAFE_ACTIONS)
    LichAgentBridge.const_set(:SAFE_ACTIONS, (old_patterns + registry.safe_patterns).freeze)
    LichAgentBridge.instance_variable_set(:@controlled_runs, {})
    child = SyntheticQuickChild.new('bigshot')
    runtime = SyntheticQuickRuntime.new(child)
    runtime.current_status = { mode: 'watch', state: :running, area: synthetic_quick_area } if area
    child.quick_combat_runtime = runtime
    action = { action_id: '0123456789abcdef', character: 'Testmage', generation: old_generation,
               command: 'lab-test-quick start', expected_room_id: '1000', expires_at: Time.now.to_f + 1,
               controller_deadline: Time.now.to_f + 30 }
    fixture = { child: child, runtime: runtime, action: action, http: [], results: [], starts: [], ordinary_launches: [], start_delay: 0, room: '1000', state_overrides: {}, standing: true }
    fixture[:authority] = { action[:action_id] => action.merge(status: 'dispatched', stop_requested: false, return_requested: false) }
    replace.call(Script, :running) { child.alive ? [child] : [] }
    replace.call(Script, :running?) { |name| fixture[:started] && child.alive && child.name == name }
    replace.call(LichAgentBridge, :supervised_controller_supported?) { |_name| true }
    replace.call(LichAgentBridge, :launch_controller) do |controller, action, arguments|
      fixture[:ordinary_launches] << [controller.name, action.name, arguments]
      false
    end
    replace.call(Script, :start_child) do |name, arguments, options|
      fixture[:starts] << [name, arguments, options]
      fixture[:started] = true
      tokens = arguments.split
      work, cleanup = tokens[tokens.index('--supervised-start-v1') + 1].split(',').map { |part| Float(part) }
      runtime.bind_execution_window(work_deadline: work, cleanup_deadline: cleanup)
      sleep fixture[:start_delay] if fixture[:start_delay].positive?
      child
    end
    replace.call(LichAgentBridge, :request) do |verb, path, payload = nil, **options|
      fixture[:http] << [verb, path, payload, options, Thread.current]
      path == '/v1/actions/status' ? fixture[:authority][payload[:action_id]]&.dup : {}
    end
    replace.call(LichAgentBridge, :action_request) { |path, payload| fixture[:results] << [path, payload]; {} }
    replace.call(LichAgentBridge, :publish_snapshot) { |force: false| force }
    replace.call(LichAgentBridge, :room_fingerprint) { fixture[:room] }
    original_snapshot = LichAgentBridge.method(:snapshot_state)
    replace.call(LichAgentBridge, :snapshot_state) do |**options|
      original_snapshot.call(**options).merge(room: { id: fixture[:room] }).merge(fixture[:state_overrides])
    end
    original_indicators = XMLData.method(:indicator)
    replace.call(XMLData, :indicator) { original_indicators.call.merge('IconSTANDING' => fixture[:standing] ? 'y' : 'n') }
    original_monitor = LichAgentBridge.method(:monitor_controlled_run)
    replace.call(LichAgentBridge, :monitor_controlled_run) { |run| original_monitor.call(run, cleanup_timeout: 0.05) }
    yield fixture
  ensure
    child.alive = false if child
    runs = LichAgentBridge.instance_variable_get(:@controlled_runs) || {}
    runs.each_value { |run| run[:monitor]&.join(1) }
    originals&.each do |(object, name), method|
      method ? object.define_singleton_method(name, method) : object.singleton_class.send(:remove_method, name)
    end
    if old_registry
      LichAgentBridge.send(:remove_const, :CONTROLLER_REGISTRY)
      LichAgentBridge.const_set(:CONTROLLER_REGISTRY, old_registry)
      LichAgentBridge.send(:remove_const, :SAFE_ACTIONS)
      LichAgentBridge.const_set(:SAFE_ACTIONS, old_patterns)
      LichAgentBridge.instance_variable_set(:@controlled_runs, old_runs)
      LichAgentBridge.instance_variable_set(:@session_generation, old_generation)
    end
  end

  def quick_control_action(fixture, verb = 'hold', token: fixture[:action][:action_id])
    action = { action_id: 'bbbbbbbbbbbbbbbb', character: 'Testmage', generation: fixture[:action][:generation],
               command: "lab-test-quick #{verb} #{token}", expected_room_id: '1000', expires_at: Time.now.to_f + 1 }
    fixture[:authority][action[:action_id]] = action.merge(status: 'dispatched', stop_requested: false)
    action
  end

  def with_quick_loader_ownership
    with_controlled_quick(inventory: true) do |fixture|
      match = LichAgentBridge::CONTROLLER_REGISTRY.match(fixture[:action][:command])
      run = { instance: fixture[:child], runtime: fixture[:runtime], controller: match.controller, action: fixture[:action] }
      LichAgentBridge.instance_variable_get(:@controlled_runs)[match.controller.name] = run
      fixture[:children], fixture[:other_scripts] = [], []
      fixture[:child].define_singleton_method(:child_scripts) { fixture[:children].dup }
      Script.define_singleton_method(:running) { [fixture[:child]] + fixture[:other_scripts] }
      yield fixture, -> { LichAgentBridge.controlled_local_available?(run, fixture[:action], check_room: false) }
    end
  end

  def test_pending_exact_owned_eloot_definitions_loader_preserves_quick_ownership
    with_quick_loader_ownership do |fixture, available|
      assert available.call
      # Native Script.vars contains the full argument string followed by parsed arguments.
      loader = Struct.new(:name, :vars).new('eloot', ['--load-room-api', '--load-room-api'])
      fixture[:children] << loader
      fixture[:other_scripts] << loader
      3.times { assert available.call, 'the pending owned definitions loader must not revoke Quick' }
      assert_includes LichAgentBridge.live_scripts, 'eloot', 'snapshot visibility is unchanged'

      # Struct equality is deliberately insufficient: only exact native child identity counts.
      foreign = loader.dup
      assert_equal loader, foreign
      fixture[:other_scripts] << foreign
      refute available.call, 'a simultaneous foreign definitions loader must still conflict'
      fixture[:other_scripts].delete_at(1)
      assert available.call
      fixture[:children].clear
      refute available.call, 'a no-longer-owned loader is not exempt'
    end
  end

  def test_eloot_loader_exemption_requires_direct_ownership_and_complete_native_arguments
    variants = [
      [false, ['--load-room-api', '--load-room-api']],
      [true, []],
      [true, ['--load-room-api']],
      [true, ['--load-room-api normal', '--load-room-api', 'normal']],
      [true, ['normal --load-room-api', 'normal', '--load-room-api']],
      [true, ['--load-room-api ', '--load-room-api']],
      [true, ['loot', 'loot']]
    ]
    variants.each do |direct, args|
      with_quick_loader_ownership do |fixture, available|
        loader = Struct.new(:name, :vars).new('eloot', args)
        fixture[:other_scripts] << loader
        fixture[:children] << loader if direct
        # A child of another child is not the bound Quick's direct definitions loader.
        unless direct
          sibling = Struct.new(:name, :child_scripts).new('helper', [loader])
          fixture[:children] << sibling
        end
        refute available.call, "unexpected exemption: direct=#{direct}, vars=#{args.inspect}"
      end
    end
  end

  def test_hidden_eloot_loader_uses_the_same_exact_identity_check
    original_hidden = Script.method(:hidden) if Script.respond_to?(:hidden)
    with_quick_loader_ownership do |fixture, available|
      loader = Struct.new(:name, :vars).new('eloot', ['--load-room-api', '--load-room-api'])
      fixture[:children] << loader
      hidden = [loader]
      Script.define_singleton_method(:hidden) { hidden }
      assert available.call
      hidden << Struct.new(:name, :vars).new('eloot', [])
      refute available.call, 'normal hidden eLoot remains an inventory conflict'
    end
  ensure
    original_hidden ? Script.define_singleton_method(:hidden, original_hidden) : Script.singleton_class.send(:remove_method, :hidden)
  end

  def test_exact_active_owned_go2_preserves_quick_ownership_without_hiding_foreign_go2
    with_quick_loader_ownership do |fixture, available|
      traveler = Struct.new(:name).new('go2')
      fixture[:children] << traveler
      fixture[:other_scripts] << traveler
      fixture[:active_traveler] = traveler
      fixture[:child].define_singleton_method(:quick_refuge_travel_child?) do |candidate|
        candidate.equal?(fixture[:active_traveler])
      end
      assert available.call, 'the exact owned active transit child belongs to Quick'
      assert_includes LichAgentBridge.live_scripts, 'go2', 'snapshot visibility is unchanged'

      foreign = traveler.dup
      assert_equal traveler, foreign
      fixture[:other_scripts] << foreign
      refute available.call, 'same name/value is not exact child identity'
      fixture[:other_scripts].delete_at(1)
      assert available.call

      fixture[:active_traveler] = nil
      refute available.call, 'a stale child-list entry cannot authorize an inactive traveler'
      fixture[:active_traveler] = traveler
      fixture[:children].clear
      refute available.call, 'a stale predicate cannot authorize a no-longer-owned child'
    end
  end

  def test_go2_exemption_requires_direct_child_exact_name_and_literal_positive_predicate
    [nil, false, :active, 'true', :raises].each do |decision|
      with_quick_loader_ownership do |fixture, available|
        traveler = Struct.new(:name).new('go2')
        fixture[:children] << traveler
        fixture[:other_scripts] << traveler
        fixture[:child].define_singleton_method(:quick_refuge_travel_child?) do |_candidate|
          raise 'unavailable travel observation' if decision == :raises
          decision
        end
        refute available.call, "non-positive/unreadable predicate must conflict: #{decision.inspect}"
      end
    end
    with_quick_loader_ownership do |fixture, available|
      traveler = Struct.new(:name).new('go2')
      fixture[:other_scripts] << traveler
      fixture[:child].define_singleton_method(:quick_refuge_travel_child?) { |_candidate| true }
      fixture[:children] << Struct.new(:name, :child_scripts).new('helper', [traveler])
      refute available.call, 'even a positive predicate cannot exempt a sibling/foreign child'
      fixture[:children].replace([traveler])
      traveler.name = 'eloot'
      refute available.call, 'the travel predicate cannot whitelist another script'
    end
    with_quick_loader_ownership do |fixture, available|
      traveler = Struct.new(:name).new('go2')
      fixture[:children] << traveler
      fixture[:other_scripts] << traveler
      refute available.call, 'older owners without the travel predicate must fail closed'
    end
  end

  def test_hidden_go2_requires_the_same_exact_owned_active_traveler
    original_hidden = Script.method(:hidden) if Script.respond_to?(:hidden)
    with_quick_loader_ownership do |fixture, available|
      traveler = Struct.new(:name).new('go2')
      fixture[:children] << traveler
      fixture[:child].define_singleton_method(:quick_refuge_travel_child?) { |candidate| candidate.equal?(traveler) }
      hidden = [traveler]
      Script.define_singleton_method(:hidden) { hidden }
      assert available.call
      hidden << traveler.dup
      refute available.call, 'a hidden foreign go2 remains a movement conflict'
    end
  ensure
    original_hidden ? Script.define_singleton_method(:hidden, original_hidden) : Script.singleton_class.send(:remove_method, :hidden)
  end

  def synthetic_quick_area(room_id = 1000)
    { kind: :profile, start_room_id: 1000, boundary_room_ids: [1009], room_count: 3,
      room_id: room_id, in_bounds: true }
  end

  def test_area_watch_and_assist_follow_player_with_each_control_still_room_pinned
    %w[watch assist seek].each do |mode|
      with_controlled_quick(area: true) do |fixture|
        LichAgentBridge.execute_action(fixture[:action])
        run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
        fixture[:runtime].current_status = { mode: mode, state: :running, area: synthetic_quick_area(1001) }
        fixture[:room] = '1001'
        action = quick_control_action(fixture)
        action[:expected_room_id] = '1001'
        fixture[:authority][action[:action_id]][:expected_room_id] = '1001'
        LichAgentBridge.execute_action(action)
        assert_equal 'hold', fixture[:runtime].requests.first.first
        predicate = fixture[:runtime].requests.first[1]
        assert predicate.call
        assert run[:binding].open?
        fixture[:room] = '1002'
        fixture[:runtime].current_status = { mode: mode, state: :running, area: synthetic_quick_area(1002) }
        refute predicate.call, 'queued control cannot follow further movement'
      end
    end
  end

  def test_refuge_outing_tracks_phase_while_each_control_remains_room_pinned
    with_controlled_quick(area: true) do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:room] = '1001'
      refute LichAgentBridge.controlled_local_available?(run, fixture[:action]), 'an old-room control cannot follow movement'
      %w[outbound working recovering returning].each do |phase|
        fixture[:runtime].current_status = { state: :running, refuge: { room_id: 1000, phase: phase } }
        assert LichAgentBridge.controlled_run_room_available?(run)
      end
      fixture[:runtime].current_status = { mode: 'watch', state: :running, area: synthetic_quick_area(1001) }
      assert LichAgentBridge.controlled_run_room_available?(run)
      fixture[:child].quick_combat_runtime = SyntheticQuickRuntime.new(fixture[:child])
      refute LichAgentBridge.controlled_local_available?(run, fixture[:action].merge(expected_room_id: '1001'))
    end
  end

  def test_area_cached_room_lag_denies_controls_without_closing_native_run
    with_controlled_quick(area: true) do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:room] = '1001'
      refute LichAgentBridge.controlled_local_available?(run, fixture[:action])
      assert LichAgentBridge.controlled_monitor_room_available?(run)
      sleep 0.12 # Observe more than two actual bridge monitor iterations.
      assert_nil run[:failure]
      assert run[:binding].open?
      assert_empty fixture[:runtime].requests
      fixture[:runtime].current_status = { mode: 'watch', state: :running, area: synthetic_quick_area(1001) }
      action = quick_control_action(fixture)
      action[:expected_room_id] = '1001'
      fixture[:authority][action[:action_id]][:expected_room_id] = '1001'
      LichAgentBridge.execute_action(action)
      assert_equal 'hold', fixture[:runtime].requests.first.first
      assert fixture[:runtime].requests.first[1].call
    end
  end

  def test_unknown_refuge_phase_stops_without_claiming_a_safe_handoff
    with_controlled_quick(area: true) do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:runtime].current_status = { mode: 'watch', state: :running,
        area: synthetic_quick_area(1009).merge(in_bounds: false), refuge: { room_id: 1000, phase: 'unknown' } }
      fixture[:room] = '1009'
      assert run[:monitor].join(1)
      assert_includes fixture[:runtime].requests.map(&:first), 'stop'
      assert_equal 'controller_room_changed', run[:failure]
      refute run[:result][:ok]
      assert_equal 'unsafe_handoff', run[:result][:code]
    end
  end

  def test_area_exit_during_admitted_retreat_preserves_separate_refuge_cleanup
    with_controlled_quick(area: true) do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:runtime].current_status = { mode: 'watch', state: :held, retreat_pending: true,
        area: synthetic_quick_area(1009).merge(in_bounds: false), refuge: { room_id: 1000, phase: 'returning' } }
      fixture[:room] = '1009'
      sleep 0.12
      assert run[:binding].open?
      refute_includes fixture[:runtime].requests.map(&:first), 'stop'
      fixture[:room] = '1000'
      fixture[:runtime].current_status = { state: :completed, reason: 'room_clear' }
      fixture[:child].alive = false
      assert run[:monitor].join(1)
      assert run[:result][:details][:recovery_complete]
    end
  end

  def test_controlled_inventory_lane_is_owned_by_the_registered_controller
    with_controlled_quick(inventory: true) do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      assert run[:binding]&.open?, 'declared inventory ownership must not revoke a valid launch'
      assert run[:lease].valid?
      assert_equal 'bigshot', LichAgentBridge.script_owners(['bigshot'])[:inventory]
      assert_equal 'completed', fixture[:results].last.last[:outcome]
      assert_empty fixture[:runtime].requests, 'startup must not queue a spurious stop'
      %w[eloot eherbs lab-inventory].each do |other|
        assert_equal other, LichAgentBridge.script_owners(['bigshot', other])[:inventory]
      end
    end
  end

  def test_passive_inventory_observer_does_not_claim_the_inventory_lane
    previous = Object.const_get(:LabInventory) if Object.const_defined?(:LabInventory)
    Object.send(:remove_const, :LabInventory) if previous
    tracker = Module.new
    active = false
    tracker.define_singleton_method(:inventory_lane_active?) { active }
    Object.const_set(:LabInventory, tracker)
    assert_nil LichAgentBridge.script_owners(['lab-inventory'])[:inventory]
    active = true
    assert_equal 'lab-inventory', LichAgentBridge.script_owners(['lab-inventory'])[:inventory]
    assert_equal 'lab-inventory', LichAgentBridge.script_owners([])[:inventory], 'refresh workers retain ownership if the passive observer exits'
    tracker.define_singleton_method(:inventory_lane_active?) { raise 'unavailable' }
    assert_equal 'lab-inventory', LichAgentBridge.script_owners(['lab-inventory'])[:inventory]
  ensure
    Object.send(:remove_const, :LabInventory) if Object.const_defined?(:LabInventory)
    Object.const_set(:LabInventory, previous) if previous
  end

  def test_controlled_bridge_pins_native_child_and_routes_control_without_owner_transport_calls
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      assert_same fixture[:child], run[:instance]
      assert_same fixture[:runtime], run[:runtime]
      assert_equal 'bigshot', fixture[:starts].first[0]
      assert_match(/--supervised-start-v1 \d+\.\d+,\d+\.\d+ --supervised-refuge-v1 1000,\d+\.\d+\z/, fixture[:starts].first[1])
      assert_equal({ quiet: true }, fixture[:starts].first[2])
      LichAgentBridge.execute_action(quick_control_action(fixture))
      assert_equal 'hold', fixture[:runtime].requests.first.first
      callback = fixture[:runtime].requests.first[1]
      reads_before = fixture[:http].count { |item| item[1] == '/v1/actions/status' && item.last.equal?(Thread.current) }
      assert_equal true, callback.call
      reads_after = fixture[:http].count { |item| item[1] == '/v1/actions/status' && item.last.equal?(Thread.current) }
      assert_equal reads_before, reads_after
      assert_equal 'sent_unverified', fixture[:results].last.last[:outcome]
      control_event = fixture[:http].find { |item| item[1] == '/v1/event' && item[2][:data][:code] == 'control_queued' }
      assert_nil control_event[2][:data][:details][:applied]
      fixture[:runtime].current_status = { state: :completed, reason: 'sequence_dispatched' }
      fixture[:child].alive = false
      assert run[:monitor].join(1)
      assert_equal 'quick_sequence_dispatched', run[:result][:code]
      assert_equal false, run[:result][:details][:effects_verified]
    end
  end

  def test_controlled_status_uses_the_same_bounded_presentation_without_changing_control_identity
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      source = synthetic_large_quick_status.merge(state: :running, reason: nil)
      fixture[:runtime].current_status = source
      action = quick_control_action(fixture)
      LichAgentBridge.execute_action(action)
      event = fixture[:http].find { |item| item[1] == '/v1/event' && item[2][:data][:code] == 'control_queued' }
      refute_nil event
      details = event[2][:data][:details]
      assert_equal fixture[:action][:action_id], details[:run_id]
      assert_equal action[:action_id], details[:control_action_id]
      assert_nil details[:applied]
      assert_equal 110, details[:status][:observations_total]
      assert_equal 'json', details[:status][:observations].last[:command_presentation]
      assert_operator JSON.generate(details[:status]).bytesize, :<=, 32_768
      assert_instance_of Array, source[:observations].last[:command]
    end
  end

  def test_controlled_run_lifetime_does_not_expire_with_its_dispatch_window
    with_controlled_quick do |fixture|
      fixture[:action][:expires_at] = Time.now.to_f + 0.005
      fixture[:authority][fixture[:action][:action_id]][:expires_at] = fixture[:action][:expires_at]
      fixture[:start_delay] = 0.01
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      assert run[:binding]&.open?
      assert run[:lease].valid?
      assert_operator Time.now.to_f, :>, fixture[:action][:expires_at]
      assert_equal 'completed', fixture[:results].last.last[:outcome]
    end
  end

  def test_refuge_terminal_observation_is_distinct_from_stop_or_verified_game_effects
    with_controlled_quick do |fixture|
      fixture[:child].alive = false
      run = { instance: fixture[:child], runtime: fixture[:runtime], action: fixture[:action] }
      %w[retreated already_at_refuge manual_stop retreat_unconfirmed execution_error].each do |reason|
        fixture[:runtime].current_status = { state: :stopped, reason: reason }
        result = LichAgentBridge.controlled_terminal_result(run, true)
        assert_equal %w[retreated already_at_refuge].include?(reason), result[:ok]
        assert_equal "quick_#{reason}", result[:code]
        assert_equal false, result[:details][:effects_verified]
      end
      refute LichAgentBridge.controlled_terminal_result(run, false)[:ok]
    end
  end

  def test_controlled_launch_rejects_missing_operation_deadline_before_spawn
    with_controlled_quick do |fixture|
      fixture[:action].delete(:controller_deadline)
      LichAgentBridge.execute_action(fixture[:action])
      assert_empty fixture[:starts]
      assert_equal 'failed', fixture[:results].last.last[:outcome]
    end
  end

  def test_old_room_and_field_only_quick_registrations_load_but_cannot_launch
    [{ 'kind' => 'room', 'room_id' => '1000' }, { 'kind' => 'quick_area' }].each do |legacy|
      with_controlled_quick(legacy: legacy) do |fixture|
        LichAgentBridge.execute_action(fixture[:action])
        assert_empty fixture[:starts]
        assert_equal 'failed', fixture[:results].last.last[:outcome]
        LichAgentBridge.execute_action(quick_control_action(fixture))
        assert_equal 'failed', fixture[:results].last.last[:outcome]
      end
    end
  end

  def test_uncontrolled_quick_cannot_use_ordinary_controller_launch_but_non_quick_still_can
    %w[quick QUICK hunt].each do |mode|
      with_controlled_quick(legacy: { 'kind' => 'room', 'room_id' => '1000' },
                           uncontrolled: true, script_args: "#{mode} clear") do |fixture|
        LichAgentBridge.execute_action(fixture[:action])
        assert_empty fixture[:starts]
        assert_equal mode == 'hunt' ? 1 : 0, fixture[:ordinary_launches].length
      end
    end
  end

  def test_refuge_start_requires_fresh_refuge_survival_posture_and_known_hands
    mutations = [
      ->(f) { f[:room] = '1001'; f[:action][:expected_room_id] = '1001' },
      ->(f) { f[:state_overrides][:generation] = 'stale-session' },
      ->(f) { f[:state_overrides][:dead] = true },
      ->(f) { f[:state_overrides][:stunned] = true },
      ->(f) { f[:state_overrides][:hands] = { right: { name: 'unknown identity' }, left: nil } },
      ->(f) { f[:standing] = false },
      ->(f) { f[:action][:controller_deadline] = Time.now.to_f + 301 },
      ->(f) { f[:action][:controller_deadline] = Time.now.to_f + 20 }
    ]
    mutations.each do |mutate|
      with_controlled_quick do |fixture|
        mutate.call(fixture)
        fixture[:authority][fixture[:action][:action_id]].merge!(fixture[:action])
        LichAgentBridge.execute_action(fixture[:action])
        assert_empty fixture[:starts]
        assert_equal 'failed', fixture[:results].last.last[:outcome]
      end
    end
  end

  def test_refuge_preflight_rejection_publishes_proof_that_no_child_started
    with_controlled_quick do |fixture|
      fixture[:standing] = false

      LichAgentBridge.execute_action(fixture[:action])

      assert_empty fixture[:starts]
      assert_equal 'failed', fixture[:results].last.last[:outcome]
      event = fixture[:http].find do |entry|
        entry[1] == '/v1/event' && entry[2].dig(:data, :code) == 'controlled_start_rejected'
      end
      refute_nil event
      assert_equal fixture[:action][:action_id], event[2].dig(:data, :action_id)
      assert_includes event[2].dig(:data, :message), 'must be standing'
      assert_equal false, event[2].dig(:data, :details, :child_started)
      assert_equal true, event[2].dig(:data, :details, :cleanup_complete)
      assert_equal false, event[2].dig(:data, :details, :effects_verified)
    end
  end

  def test_ordinary_return_request_keeps_cached_authority_and_is_queued_once
    with_controlled_quick do |fixture|
      fixture[:runtime].auto_stop = false
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:authority][fixture[:action][:action_id]][:return_requested] = true
      Timeout.timeout(1) { sleep 0.005 until run[:return_requested] }
      sleep 0.12
      assert run[:lease].valid?
      assert run[:binding].open?
      assert_equal ['stop'], fixture[:runtime].requests.map(&:first)
      assert fixture[:runtime].requests.first[1].call
      fixture[:authority][fixture[:action][:action_id]][:stop_requested] = true
      assert run[:monitor].join(1)
      refute fixture[:runtime].requests.first[1].call, 'hard revocation still denies return commands'
      refute run[:result][:ok]
      assert run[:unsafe_handoff]
    end
  end

  def test_refuge_recovery_does_not_turn_failed_or_missing_work_into_test_success
    [{ state: :held, reason: 'wounded' }, nil].each do |work|
      with_controlled_quick do |fixture|
        LichAgentBridge.execute_action(fixture[:action])
        run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
        fixture[:runtime].current_status = { state: :completed, reason: 'returned', work_result: work }
        fixture[:child].alive = false
        assert run[:monitor].join(1)
        assert run[:result][:details][:recovery_complete]
        refute run[:result][:ok]
        refute run[:unsafe_handoff]
      end
    end
  end

  def test_refuge_terminal_proof_is_not_a_substitute_for_fresh_hands_posture_and_location
    mutations = [
      ->(f) { f[:room] = '1001' },
      ->(f) { f[:standing] = false },
      ->(f) { f[:state_overrides][:hands] = { right: nil, left: { id: '999' } } },
      ->(f) { f[:state_overrides][:dead] = true },
      ->(f) { f[:state_overrides][:generation] = 'replacement' },
      ->(f) { f[:runtime].current_status[:refuge] = { room_id: 1000, phase: 'finished', returned: true, equipment_restored: false } },
      ->(f) { f[:runtime].current_status[:refuge] = { room_id: 1001, phase: 'finished', returned: true, equipment_restored: true } }
    ]
    mutations.each do |mutate|
      with_controlled_quick do |fixture|
        LichAgentBridge.execute_action(fixture[:action])
        run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
        fixture[:runtime].current_status = { state: :completed, reason: 'room_clear' }
        mutate.call(fixture)
        fixture[:child].alive = false
        assert run[:monitor].join(1)
        refute run[:result][:ok]
        assert run[:unsafe_handoff]
        assert_same run, LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      end
    end
  end

  def test_joined_unsafe_handoff_blocks_new_quick_until_same_session_refuge_hands_and_owners_are_verified
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:runtime].current_status = { state: :stopped, reason: 'return_blocked',
        refuge: { room_id: 1000, phase: 'finished', returned: false, equipment_restored: false } }
      fixture[:room] = '1001'
      fixture[:child].alive = false
      assert run[:monitor].join(1)
      assert LichAgentBridge.unresolved_controlled_handoff?
      match = LichAgentBridge::CONTROLLER_REGISTRY.match(fixture[:action][:command])
      LichAgentBridge.launch_controlled_controller(fixture[:action], match)
      assert_equal 1, fixture[:starts].length
      fixture[:room] = '1000'
      fixture[:state_overrides][:hands] = { right: { id: '999' }, left: nil }
      assert LichAgentBridge.unresolved_controlled_handoff?
      fixture[:state_overrides].clear
      foreign = Struct.new(:name).new('eloot')
      Script.define_singleton_method(:running) { [foreign] }
      assert LichAgentBridge.unresolved_controlled_handoff?
      Script.define_singleton_method(:running) { [] }
      old_generation = LichAgentBridge.session_generation
      LichAgentBridge.instance_variable_set(:@session_generation, 'new-session')
      assert LichAgentBridge.unresolved_controlled_handoff?
      LichAgentBridge.instance_variable_set(:@session_generation, old_generation)
      refute LichAgentBridge.unresolved_controlled_handoff?
      assert_empty LichAgentBridge.instance_variable_get(:@controlled_runs)
    end
  end

  def test_monitor_exception_retains_joined_unsafe_latch_and_reports_failure
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      LichAgentBridge.define_singleton_method(:publish_snapshot) { |**| raise 'synthetic snapshot failure' }
      fixture[:runtime].current_status = { state: :completed, reason: 'room_clear' }
      fixture[:child].alive = false
      assert run[:monitor].join(1)
      assert run[:unsafe_handoff]
      refute run[:result][:ok]
      assert_equal 'controller_monitor_failed', run[:result][:code]
      assert_same run, LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      event = fixture[:http].find { |entry| entry[1] == '/v1/event' && entry[2][:data][:code] == 'controller_monitor_failed' }
      refute_nil event
    end
  end

  def test_unacknowledged_safe_terminal_result_retains_recoverable_run_and_original_result
    original_publisher = LichAgentBridge.method(:publish_controller_result)
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      LichAgentBridge.define_singleton_method(:publish_controller_result) { |_controller, _id, _result| false }
      fixture[:runtime].current_status = { state: :completed, reason: 'room_clear' }
      fixture[:child].alive = false

      assert run[:monitor].join(1)
      assert run[:result][:ok]
      assert_equal 'quick_room_clear', run[:result][:code]
      assert run[:unsafe_handoff]
      assert_equal true, run[:result_publication_failed]
      assert_same run, LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
    end
  ensure
    LichAgentBridge.define_singleton_method(:publish_controller_result, original_publisher) if original_publisher
  end

  def test_controlled_launch_binds_fixed_work_and_cleanup_deadlines
    with_controlled_quick do |fixture|
      fixture[:action][:controller_deadline] = Time.now.to_f + 30
      fixture[:authority][fixture[:action][:action_id]][:controller_deadline] = fixture[:action][:controller_deadline]
      before = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      LichAgentBridge.execute_action(fixture[:action])
      window = fixture[:runtime].execution_window
      refute_nil window
      assert_in_delta 10, window[:cleanup_deadline] - window[:work_deadline], 0.01
      assert_operator window[:work_deadline], :>, before + 7
      assert_operator window[:cleanup_deadline], :<=, before + 18.1
    end
  end

  def test_controlled_launch_refuses_unsupported_historical_script_before_spawning
    with_controlled_quick do |fixture|
      LichAgentBridge.define_singleton_method(:supervised_controller_supported?) { |_| false }
      LichAgentBridge.execute_action(fixture[:action])
      assert_empty fixture[:starts]
      assert_equal 'failed', fixture[:results].last.last[:outcome]
    end
  end

  def test_supervised_preflight_reads_only_the_script_resolved_by_lich
    previous_root = $script_dir
    previous_resolver = Script.method(:__find_script_file) if Script.respond_to?(:__find_script_file, true)
    Dir.mktmpdir('supervised-quick-protocol') do |root|
      $script_dir = root
      FileUtils.mkdir_p(File.join(root, 'custom'))
      path = File.join(root, 'custom', 'bigshot.lic')
      Script.define_singleton_method(:__find_script_file) { |_| 'custom/bigshot.lic' }
      File.write(path, "raise 'never evaluate preflight source'\n")
      refute LichAgentBridge.supervised_quick_supported?('bigshot')
      File.write(path, "    SUPERVISED_START_PROTOCOL = 1\r\nraise 'never evaluate preflight source'\n")
      refute LichAgentBridge.supervised_quick_supported?('bigshot')
      File.write(path, "    SUPERVISED_START_PROTOCOL = 1\r\n    REFUGE_START_PROTOCOL = 1\r\nraise 'never evaluate preflight source'\n")
      assert LichAgentBridge.supervised_quick_supported?('bigshot')
      File.write(path, "    SUPERVISED_CONTROLLER_PROTOCOL = 1\r\n    REFUGE_START_PROTOCOL = 1\r\nraise 'never evaluate preflight source'\n")
      assert LichAgentBridge.supervised_controller_supported?('eohunter')
    end
  ensure
    $script_dir = previous_root
    if previous_resolver
      Script.define_singleton_method(:__find_script_file, previous_resolver)
    elsif Script.respond_to?(:__find_script_file, true)
      Script.singleton_class.remove_method(:__find_script_file)
    end
  end

  def test_generic_controller_publications_take_precedence_over_quick_compatibility
    child = SyntheticQuickChild.new('eohunter')
    generic_runtime = Object.new
    generic_result = { state: :completed }.freeze
    child.controller_runtime = generic_runtime
    child.controller_result = generic_result
    child.quick_combat_runtime = Object.new
    child.quick_combat_result = { state: :stopped }.freeze

    assert_same generic_runtime, LichAgentBridge.controlled_runtime(child)
    assert_same generic_result, LichAgentBridge.controlled_terminal_status(child)
  end

  def test_controlled_launch_stops_a_runtime_that_refuses_deadline_coordination
    with_controlled_quick do |fixture|
      fixture[:runtime].define_singleton_method(:activate_supervised) { |**| false }
      LichAgentBridge.execute_action(fixture[:action])
      assert_equal 'failed', fixture[:results].last.last[:outcome]
      assert_includes fixture[:runtime].requests.map(&:first), 'stop'
      refute fixture[:child].running?
    end
  end

  def test_child_finishing_during_lease_refresh_preserves_its_budget_failure
    with_controlled_quick do |fixture|
      match = LichAgentBridge::CONTROLLER_REGISTRY.match(fixture[:action][:command])
      lease = Object.new
      lease.define_singleton_method(:refresh) do
        fixture[:runtime].current_status = { state: :stopped, reason: 'command_limit' }
        fixture[:child].alive = false
        false
      end
      binding = Object.new
      binding.define_singleton_method(:close) { nil }
      binding.define_singleton_method(:open?) { false }
      run = { instance: fixture[:child], runtime: fixture[:runtime], action: fixture[:action],
              controller: match.controller, lease: lease, binding: binding, start_hands: { right: nil, left: nil } }
      assert LichAgentBridge.monitor_controlled_run(run).join(1)
      assert_equal 'quick_command_limit', run[:result][:code]
      refute run[:result][:ok]
      assert run[:result][:details][:cleanup_complete]
      assert_empty fixture[:runtime].requests
    end
  end

  def test_child_finishing_before_supervised_activation_is_not_admitted_as_a_success
    with_controlled_quick do |fixture|
      fixture[:child].quick_combat_runtime = nil
      fixture[:child].quick_combat_result = { state: :completed, reason: 'sequence_dispatched' }.freeze
      fixture[:child].alive = false
      match = LichAgentBridge::CONTROLLER_REGISTRY.match(fixture[:action][:command])
      assert_nil LichAgentBridge.launch_controlled_controller(fixture[:action], match)
      assert_equal 'failed', fixture[:results].last.last[:outcome]
    end
  end

  def test_unpublished_startup_failure_cancels_exact_native_child_and_retains_incomplete_exclusion
    with_controlled_quick do |fixture|
      fixture[:child].quick_combat_runtime = nil
      fixture[:child].cleanup_blocked = true
      successor = SyntheticQuickChild.new('lab-test-quick')
      match = LichAgentBridge::CONTROLLER_REGISTRY.match(fixture[:action][:command])
      assert_nil LichAgentBridge.launch_controlled_controller(fixture[:action], match, startup_timeout: 0)
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      assert_equal [true], fixture[:child].kills
      assert fixture[:child].stopping?
      assert_empty successor.kills
      # Native stopping? is the startup admission boundary. Actual Bigshot tests
      # establish refusal before resets/commands; this bridge fixture only proves
      # exact cancellation and retains that stop flag across late publication.
      fixture[:child].quick_combat_runtime = fixture[:runtime]
      assert fixture[:child].stopping?
      assert run[:monitor].join(1)
      assert_equal 'forced_startup_cancelled', run[:failure]
      assert_equal false, run[:result][:details][:cleanup_complete]
      assert_same run, LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
    end
  end

  def test_old_run_token_replaced_runtime_and_session_change_reject_controls
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      LichAgentBridge.execute_action(quick_control_action(fixture, token: 'ffffffffffffffff'))
      assert_equal 'failed', fixture[:results].last.last[:outcome]
      assert_empty fixture[:runtime].requests
      LichAgentBridge.execute_action(quick_control_action(fixture))
      callback = fixture[:runtime].requests.first[1]
      fixture[:child].quick_combat_runtime = SyntheticQuickRuntime.new(fixture[:child])
      assert_equal false, callback.call
      fixture[:child].quick_combat_runtime = fixture[:runtime]
      LichAgentBridge.instance_variable_set(:@session_generation, 'replacement-session')
      assert_equal false, callback.call
    end
  end

  def test_original_launch_revocation_closes_leases_and_cooperatively_stops_exact_runtime
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      LichAgentBridge.execute_action(quick_control_action(fixture))
      fixture[:authority][fixture[:action][:action_id]][:stop_requested] = true
      assert run[:monitor].join(1)
      assert_equal false, fixture[:runtime].requests.first[1].call
      assert_includes fixture[:runtime].requests.map(&:first), 'stop'
      assert_equal false, run[:result][:ok]
      assert_equal true, run[:result][:details][:cleanup_complete]
    end
  end

  def test_admitted_retreat_room_change_is_not_preempted_and_incomplete_cleanup_retains_exact_exclusion
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:runtime].current_status = { state: :running, reason: 'retreat_pending', retreat_pending: true,
        refuge: { room_id: 1000, phase: 'returning' } }
      fixture[:room] = '1001'
      sleep 0.12
      assert run[:binding].open?
      fixture[:child].cleanup_blocked = true
      fixture[:child].alive = false
      assert run[:monitor].join(1)
      refute_includes fixture[:runtime].requests.map(&:first), 'stop'
      assert_equal 'unsafe_handoff', run[:result][:code]
      assert_equal false, run[:result][:details][:cleanup_complete]
      assert_same run, LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
    end
  end

  def test_hard_launch_revocation_requests_stop_even_during_retreat
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:runtime].current_status = { state: :held, reason: 'retreat_pending', retreat_pending: true }
      fixture[:authority][fixture[:action][:action_id]][:stop_requested] = true
      assert run[:monitor].join(1)
      assert_includes fixture[:runtime].requests.map(&:first), 'stop'
      assert_equal 'controller_authority_lost', run[:result][:code]
    end
  end

  def setup
    @requests = []
    $plain_responses = []
    $raw_responses = []
    $roundtime_waits = []
    LichAgentBridge.instance_variable_set(:@actions_enabled, true)
    LichAgentBridge.instance_variable_set(:@snapshot_generation_rejected, false)
    LichAgentBridge.instance_variable_set(:@last_action_registration_at, Time.at(0))
    LichAgentBridge.instance_variable_set(:@observed_inactive_spells, {})
    LichAgentBridge.instance_variable_set(:@script_status, {})
    LichAgentBridge.instance_variable_set(:@pending, [])
    LichAgentBridge.instance_variable_set(:@running, true)
    LichAgentBridge.instance_variable_set(:@command_queue, Queue.new)
    LichAgentBridge.instance_variable_set(:@sequence_prompt_counter, 0)
    LichAgentBridge.instance_variable_set(:@sequence_response_waiter, nil)
    LichAgentBridge.instance_variable_set(:@live_state_source, nil)
    LichAgentBridge.instance_variable_set(:@question_epoch, 0)
    LabInventory.capture_calls = []
    LabInventory.scroll_capture_calls = []
    GameObj.inventory = []
    GameObj.container_map = {}
    GameObj.right = nil
    GameObj.left = nil
    GameObj.room_npcs = []
    requests = @requests
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      requests << [path, payload]
      { character: payload.fetch(:character), enabled: payload.fetch(:enabled) }
    end
  end

  def test_player_facing_output_uses_the_familiar_stream_with_hidden_pane_fallback
    LichAgentBridge.display('thinking...')

    assert_equal [
      "<pushStream id=\"familiar\" ifClosedStyle=\"watching\"/>--- LAB: thinking...\n<popStream/>\n"
    ], $raw_responses
    assert_empty $plain_responses
  end

  def test_question_waits_for_server_deadline_plus_transport_grace
    original_request = LichAgentBridge.method(:request)
    calls = []
    LichAgentBridge.define_singleton_method(:request) do |method, path, payload = nil, **options|
      calls << [method, path, payload, options]
      path == '/health' ? { ask_timeout_seconds: 180 } : { text: 'answer' }
    end
    LichAgentBridge.ask('What is 706?')
    assert_equal 185, calls.last[3][:read_timeout]
    assert_equal '/v1/ask', calls.last[1]
    assert_includes $raw_responses.last, 'answer'
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request)
  end

  def test_snapshot_timestamp_does_not_truncate_character_observation_precision
    original_request = LichAgentBridge.method(:request)
    original_snapshot = LichAgentBridge.method(:snapshot_state)
    original_now = Time.method(:now)
    instant = Time.iso8601('2026-09-06T12:00:00.123456Z')
    calls = []
    LichAgentBridge.define_singleton_method(:snapshot_state) do |**_options|
      { character: 'Testmage', character_data: { info: { observed_at: instant.iso8601(6) } } }
    end
    LichAgentBridge.define_singleton_method(:request) do |method, path, payload = nil, **options|
      calls << payload
      {}
    end
    Time.define_singleton_method(:now) { instant }
    assert LichAgentBridge.publish_snapshot(force: true)
    payload = calls.fetch(0)
    assert_equal '2026-09-06T12:00:00.123456Z', payload[:observed_at]
    assert_operator Time.iso8601(payload[:observed_at]), :>=,
                    Time.iso8601(payload.dig(:character_data, :info, :observed_at))
  ensure
    Time.define_singleton_method(:now, original_now)
    LichAgentBridge.define_singleton_method(:snapshot_state, original_snapshot)
    LichAgentBridge.define_singleton_method(:request, original_request)
  end

  def test_question_uses_bounded_default_for_older_health_or_malformed_timeout
    original_request = LichAgentBridge.method(:request)
    [nil, '6000', 601, -1, Float::INFINITY].each do |reported|
      calls = []
      LichAgentBridge.define_singleton_method(:request) do |method, path, payload = nil, **options|
        calls << options
        path == '/health' ? { ask_timeout_seconds: reported } : { text: 'answer' }
      end
      LichAgentBridge.ask('Question')
      assert_equal 125, calls.last[:read_timeout]
    end
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request)
  end

  def test_questions_never_use_keyword_selected_recon_in_the_bridge
    original_request = LichAgentBridge.method(:request)
    ['What are my skills?', 'Could I use this?', 'History of Wehnimers Landing?'].each do |question|
      calls = []
      LichAgentBridge.define_singleton_method(:request) do |method, path, payload = nil, **options|
        calls << path
        path == '/health' ? { ask_timeout_seconds: 120 } : { text: 'answer' }
      end
      LichAgentBridge.ask(question)
      assert_equal ['/health', '/v1/ask'], calls
    end
    refute_respond_to LichAgentBridge, :character_context_categories
    refute_respond_to LichAgentBridge, :ensure_character_context
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request)
  end

  def test_forget_before_question_admission_prevents_request
    original_request = LichAgentBridge.method(:request)
    calls = []
    LichAgentBridge.define_singleton_method(:request) do |method, path, payload = nil, **options|
      calls << path
      if path == '/health'
        forget_context
        { ask_timeout_seconds: 120 }
      else
        { forgotten: true }
      end
    end
    LichAgentBridge.ask('Could I use this?')
    refute_includes calls, '/v1/ask'
    assert_includes calls, '/v1/session/forget'
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request)
  end

  def test_player_facing_output_escapes_xml_without_losing_readable_content
    LichAgentBridge.display(%(Kroderine: CS < TD & \"no ward\"; Testmage's staff.))

    assert_equal [
      "<pushStream id=\"familiar\" ifClosedStyle=\"watching\"/>" \
      "--- LAB: Kroderine: CS &lt; TD &amp; &quot;no ward&quot;; Testmage&apos;s staff.\n" \
      "<popStream/>\n"
    ], $raw_responses
  end

  def test_player_facing_output_cannot_inject_another_stream_or_game_command
    LichAgentBridge.display(%(answer</popStream><pushStream id=\"combat\"/><c>drop staff))

    frame = $raw_responses.fetch(0)
    assert_equal 1, frame.scan('<pushStream').length
    assert_equal 1, frame.scan('<popStream/>').length
    refute_includes frame, '<c>'
    refute_includes frame, '<pushStream id="combat"/>'
    assert_includes frame, 'answer&lt;/popStream&gt;&lt;pushStream id=&quot;combat&quot;/&gt;&lt;c&gt;drop staff'
  end

  def test_player_facing_output_normalizes_binary_game_bytes_before_framing
    binary_answer = "hello from LAB \xFF".b

    LichAgentBridge.display(binary_answer)

    assert_equal 1, $raw_responses.length
    assert_predicate $raw_responses.fetch(0), :valid_encoding?
    assert_equal Encoding::UTF_8, $raw_responses.fetch(0).encoding
    assert_includes $raw_responses.fetch(0), "--- LAB: hello from LAB \uFFFD"
  end

  def test_player_facing_output_preserves_valid_utf8_bytes_labeled_as_binary
    binary_answer = "You\u2019re safe at Town Well right now.".b

    LichAgentBridge.display(binary_answer)

    assert_equal 1, $raw_responses.length
    assert_includes $raw_responses.fetch(0), "--- LAB: You\u2019re safe at Town Well right now."
    refute_includes $raw_responses.fetch(0), "\uFFFD"
  end

  def test_bare_comma_prefix_asks_without_leaking_to_game_input
    questions = Queue.new
    original_ask = LichAgentBridge.method(:ask)
    LichAgentBridge.define_singleton_method(:ask) { |question| questions << question }

    assert_nil LichAgentBridge.handle_input('<c>,  what happened here?')
    assert_equal 'what happened here?', Timeout.timeout(1) { questions.pop }
  ensure
    LichAgentBridge.define_singleton_method(:ask, original_ask) if defined?(original_ask)
  end

  def test_comma_elsewhere_passes_through_unchanged
    input = '<c>say hello, friend'

    assert_equal input, LichAgentBridge.handle_input(input)
  end

  def test_empty_comma_prints_usage_and_is_swallowed
    assert_nil LichAgentBridge.handle_input('<c>,')
    assert_includes $raw_responses.last, 'usage: , QUESTION'
  end

  def test_legacy_sol_prefix_explains_the_rebrand_and_is_swallowed
    assert_nil LichAgentBridge.handle_input('<c>,sol status')
    assert_includes $raw_responses.last, 'Sol was renamed'
  end

  def test_management_words_after_bare_comma_are_agent_questions
    questions = Queue.new
    original_ask = LichAgentBridge.method(:ask)
    LichAgentBridge.define_singleton_method(:ask) { |question| questions << question }

    assert_nil LichAgentBridge.handle_input('<c>, status')
    assert_equal 'status', Timeout.timeout(1) { questions.pop }
  ensure
    LichAgentBridge.define_singleton_method(:ask, original_ask) if defined?(original_ask)
  end

  def test_inventory_tracker_startup_does_not_block_bridge_initialization
    started = []
    Script.define_singleton_method(:running?) { |_name| false }
    Script.define_singleton_method(:start) do |name, options|
      started << [name, options]
      Object.new
    end
    Script.define_singleton_method(:run) { |*_args| flunk('blocking Script.run must not be used') }

    assert LichAgentBridge.ensure_inventory_tracker
    assert_equal [['lab-inventory', { quiet: true }]], started
  ensure
    Script.singleton_class.send(:remove_method, :running?) if Script.respond_to?(:running?)
    Script.singleton_class.send(:remove_method, :start) if Script.respond_to?(:start)
    Script.singleton_class.send(:remove_method, :run) if Script.respond_to?(:run)
  end

  def test_inventory_tracker_startup_reuses_running_tracker
    Script.define_singleton_method(:running?) { |_name| true }
    Script.define_singleton_method(:start) { |*_args| flunk('must not start a duplicate tracker') }

    assert LichAgentBridge.ensure_inventory_tracker
  ensure
    Script.singleton_class.send(:remove_method, :running?) if Script.respond_to?(:running?)
    Script.singleton_class.send(:remove_method, :start) if Script.respond_to?(:start)
  end

  def test_dispatcher_cleanup_cannot_kill_queued_status_worker
    started = Queue.new
    release = Queue.new
    completed = Queue.new
    dispatcher_gate = Queue.new
    original_status = LichAgentBridge.method(:status)
    LichAgentBridge.define_singleton_method(:status) do
      started << true
      release.pop
      completed << true
    end

    bridge_group = nil
    bridge_loop = nil
    if LichAgentBridge.respond_to?(:command_loop)
      bridge_group = ThreadGroup.new
      bridge_loop = Thread.new { LichAgentBridge.command_loop }
      bridge_group.add(bridge_loop)
    end

    dispatcher_group = ThreadGroup.new
    dispatcher = Thread.new do
      dispatcher_gate.pop
      LichAgentBridge.handle_command('status')
    end
    dispatcher_group.add(dispatcher)
    dispatcher_gate << true
    dispatcher.join

    Timeout.timeout(1) { started.pop }
    dispatcher_group.list.each(&:kill)
    release << true

    assert_equal true, Timeout.timeout(1) { completed.pop }
  ensure
    LichAgentBridge.instance_variable_get(:@command_queue)&.push(nil)
    bridge_loop&.join(1)
    bridge_group&.list&.each(&:kill)
    dispatcher_group&.list&.each(&:kill)
    LichAgentBridge.define_singleton_method(:status, original_status) if defined?(original_status)
  end

  def test_command_queued_during_stop_does_not_start_after_shutdown
    executed = Queue.new
    stopping_queue = Object.new
    stopping_queue.define_singleton_method(:pop) do
      LichAgentBridge.instance_variable_set(:@running, false)
      proc { executed << true }
    end
    LichAgentBridge.instance_variable_set(:@command_queue, stopping_queue)

    LichAgentBridge.command_loop

    assert_raises(Timeout::Error) { Timeout.timeout(0.05) { executed.pop } }
  ensure
    LichAgentBridge.instance_variable_set(:@running, true)
    LichAgentBridge.instance_variable_set(:@command_queue, Queue.new)
  end

  def test_sources_context_and_forget_use_private_character_scoped_endpoints
    calls = []
    original_request = LichAgentBridge.method(:request)
    LichAgentBridge.define_singleton_method(:request) do |_method, path, payload = nil, **_options|
      calls << [path, payload]
      case path
      when '/v1/session/sources'
        {
          answer_available: true,
          sources: [{ title: 'Tenebrous Tether (706)', authority: 'local_gswiki', url: 'https://example.test/706', revision_id: 7061 }],
          diagnostics: [{ source: 'local_gswiki', status: 'success', detail: 'matched 1 excerpt' }]
        }
      when '/v1/session/context'
        { dialogue_turns: 1, follow_up_context_available: true, context_categories: ['reference_knowledge'] }
      when '/v1/session/forget'
        { forgotten: true }
      else
        raise "unexpected path #{path}"
      end
    end

    LichAgentBridge.show_sources
    LichAgentBridge.show_context
    LichAgentBridge.forget_context

    assert_equal [
      ['/v1/session/sources', { character: 'Testmage' }],
      ['/v1/session/context', { character: 'Testmage' }],
      ['/v1/session/forget', { character: 'Testmage' }]
    ], calls
    rendered = $raw_responses.join("\n")
    assert_includes rendered, 'Tenebrous Tether (706)'
    assert_includes rendered, 'temporary dialogue turns=1'
    assert_includes rendered, 'temporary dialogue and last-answer reference metadata cleared'
    refute_includes rendered, 'Player question:'
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request) if defined?(original_request)
  end

  def test_multiline_answers_are_individually_framed_in_the_familiar_stream
    LichAgentBridge.display("First line\nSecond line\n")

    assert_equal 2, $raw_responses.length
    assert_includes $raw_responses.fetch(0), '--- LAB: First line'
    assert_includes $raw_responses.fetch(1), '--- LAB: Second line'
    assert $raw_responses.all? { |frame| frame.start_with?('<pushStream id="familiar"') }
    assert $raw_responses.all? { |frame| frame.end_with?("<popStream/>\n") }
  end

  def test_structured_snapshot_uses_live_lich_state_and_session_generation
    staff = FakeLabObject.new('123', 'runestaff', 'training staff', 'a plain training staff', nil)
    troll = FakeLabObject.new('456', 'troll', 'massive troll king', 'a massive troll king', nil)
    corpse = FakeLabObject.new('789', 'troll', 'massive troll king', 'a massive troll king', 'dead')
    GameObj.right = staff
    GameObj.room_npcs = [troll, corpse]

    snapshot = LichAgentBridge.snapshot_state

    assert_equal 'Testmage', snapshot[:character]
    assert_match(/\A[0-9a-f]{32}\z/, snapshot[:generation])
    assert_equal({ id: '1000', title: '[Town Square Central]' }, snapshot[:room])
    assert_equal({ current: 139, max: 139 }, snapshot.dig(:vitals, :health))
    assert_equal '123', snapshot.dig(:hands, :right, :id)
    assert_equal ['456'], snapshot.dig(:nearby, :creatures).map { |entry| entry[:id] }
    assert_equal ['789'], snapshot.dig(:nearby, :corpses).map { |entry| entry[:id] }
    assert_equal({ wound: 1, scar: 0 }, snapshot.dig(:wounds, 'chest'))
  end

  def test_publish_tick_reuses_one_frontend_independent_extraction
    calls = []
    sample = Object.new
    source = Object.new
    source.define_singleton_method(:extract) do |projection_types:, hub_context:|
      calls << [:extract, projection_types, hub_context]
      sample
    end
    original_source = LichAgentBridge.instance_variable_get(:@live_state_source)
    original_snapshot = LichAgentBridge.method(:publish_snapshot)
    LichAgentBridge.instance_variable_set(:@live_state_source, source)
    LichAgentBridge.define_singleton_method(:publish_snapshot) do |force: false, extracted: nil|
      calls << [:hub, force, extracted]
      true
    end

    LichAgentBridge.publish_tick

    assert_equal :extract, calls.dig(0, 0)
    assert_empty calls.dig(0, 1)
    assert_equal LichAgentBridge.session_generation, calls.dig(0, 2, :generation)
    assert_equal [:hub, false, sample], calls.fetch(1)
  ensure
    LichAgentBridge.instance_variable_set(:@live_state_source, original_source)
    LichAgentBridge.define_singleton_method(:publish_snapshot, original_snapshot) if defined?(original_snapshot)
  end

  def test_lab_publish_tick_never_emits_frontend_specific_protocol
    original_request = LichAgentBridge.method(:request)
    original_snapshot = LichAgentBridge.method(:publish_snapshot)
    LichAgentBridge.instance_variable_set(:@live_state_source, nil)
    LichAgentBridge.define_singleton_method(:request) { |_method, _path, _payload = nil, **_options| {} }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false, extracted: nil| true }

    LichAgentBridge.publish_tick

    refute $raw_responses.any? { |frame| frame.include?('<despanaState') },
           'the frontend-independent LAB bridge must not emit a Despana protocol frame'
  ensure
    LichAgentBridge.define_singleton_method(:request, original_request) if defined?(original_request)
    LichAgentBridge.define_singleton_method(:publish_snapshot, original_snapshot) if defined?(original_snapshot)
  end

  def test_observed_dispel_messages_override_stale_lich_spell_cache
    original_active = Spell.method(:active)
    stale_spells = [401, 414, 430, 716].map { |number| FakeActiveSpell.new(number, 240) }
    Spell.define_singleton_method(:active) { stale_spells }

    LichAgentBridge.capture("You exhale the last of a virulent green mist.\n")
    LichAgentBridge.capture("The silvery luminescence fades from around you.\n")
    LichAgentBridge.capture("The tingling sensation and sense of security leaves you.\n")
    LichAgentBridge.capture("The brilliant luminescence fades from around you.\n")

    active_ids = LichAgentBridge.snapshot_state.fetch(:active_spells).map { |spell| spell.fetch(:id) }
    assert_empty active_ids & %w[401 414 430 716]
  ensure
    Spell.define_singleton_method(:active, original_active) if defined?(original_active)
  end

  def test_observed_spell_up_message_clears_the_dispel_override
    original_active = Spell.method(:active)
    stale_spells = [FakeActiveSpell.new(401, 240)]
    Spell.define_singleton_method(:active) { stale_spells }

    LichAgentBridge.capture("The silvery luminescence fades from around you.\n")
    assert_empty LichAgentBridge.snapshot_state.fetch(:active_spells)

    LichAgentBridge.capture("A silvery luminescence surrounds you.\n")
    assert_equal ['401'], LichAgentBridge.snapshot_state.fetch(:active_spells).map { |spell| spell.fetch(:id) }
  ensure
    Spell.define_singleton_method(:active, original_active) if defined?(original_active)
  end

  def test_exact_item_resolution_prefers_session_id_over_duplicate_names
    first = FakeLabObject.new('100', 'ring', 'black ring', 'an enruned black ring', nil)
    second = FakeLabObject.new('101', 'ring', 'black ring', 'an enruned black ring', nil)
    GameObj.inventory = [first, second]

    assert_equal ['101'], LichAgentBridge.resolve_exact_items('#101').map { |item| item.id.to_s }
    assert_equal 2, LichAgentBridge.resolve_exact_items('an enruned black ring').length
  end

  def test_scripted_item_examination_notifies_inventory_capture_seam
    assert LichAgentBridge.prepare_inventory_capture('read #166833005')
    assert_equal [['read #166833005', '#166833005']], LabInventory.capture_calls

    refute LichAgentBridge.prepare_inventory_capture('encumbrance')
    assert_equal 1, LabInventory.capture_calls.length
  end

  def test_scripted_scroll_operations_notify_inventory_capture_seam
    assert LichAgentBridge.prepare_inventory_capture('wave #17663526 at #17663609')
    assert LichAgentBridge.prepare_inventory_capture('infuse #17663609')

    assert_equal [
      'wave #17663526 at #17663609',
      'infuse #17663609'
    ], LabInventory.scroll_capture_calls
    assert_empty LabInventory.capture_calls
  end

  def test_unregistered_controller_commands_are_not_allowlisted
    refute LichAgentBridge.safe_action_command?('unregistered-controller status')
    refute LichAgentBridge.safe_action_command?('unregistered-controller cast 702')
  end

  def test_lab_engage_is_exact_target_and_profile_allowlisted
    assert LichAgentBridge.safe_action_command?('lab-test-engage test-living #12345')
    assert LichAgentBridge.safe_action_command?('lab-test-engage test-living #12345 loot')
    assert LichAgentBridge.safe_action_command?('lab-test-engage stop')
    assert LichAgentBridge.safe_action_command?('lab-test-room start test-living')
    assert LichAgentBridge.safe_action_command?('lab-test-room stop')
    assert LichAgentBridge.safe_action_command?('lab-test-hunt status')
    assert LichAgentBridge.safe_action_command?('lab-test-hunt start test-hunt')
    assert LichAgentBridge.safe_action_command?('lab-test-hunt start test-probe')
    assert LichAgentBridge.safe_action_command?('lab-test-hunt return')
    assert LichAgentBridge.safe_action_command?('lab-test-rift status')
    assert LichAgentBridge.safe_action_command?('lab-test-rift preflight')
    assert LichAgentBridge.safe_action_command?('lab-test-rift extract')
    assert LichAgentBridge.safe_action_command?('lab-test-rift drill')
    assert LichAgentBridge.safe_action_command?('lab-test-rift probe')
    assert LichAgentBridge.safe_action_command?('lab-test-rift patrol')
    assert LichAgentBridge.safe_action_command?('lab-test-rift hunt')

    refute LichAgentBridge.safe_action_command?('lab-test-engage test-living mastiff')
    refute LichAgentBridge.safe_action_command?('lab-test-engage other-profile #12345')
    refute LichAgentBridge.safe_action_command?('lab-test-engage test-living #12345; north')
    refute LichAgentBridge.safe_action_command?('lab-test-room start other-profile')
    refute LichAgentBridge.safe_action_command?('lab-test-hunt start other-profile')
    refute LichAgentBridge.safe_action_command?('lab-test-rift grind')
    refute LichAgentBridge.safe_action_command?('lab-test-rift drill; north')
  end

  def test_current_room_eloot_is_explicitly_allowlisted
    assert LichAgentBridge.safe_action_command?('eloot loot')
    refute LichAgentBridge.safe_action_command?('eloot loot all')
  end

  def test_eherbs_heal_is_exactly_allowlisted
    assert LichAgentBridge.safe_action_command?('eherbs heal')
    refute LichAgentBridge.safe_action_command?('eherbs')
    refute LichAgentBridge.safe_action_command?('eherbs heal; north')
  end

  def test_scroll_infusion_actions_are_narrowly_allowlisted
    assert LichAgentBridge.safe_action_command?('pour my potion on my stone')
    assert LichAgentBridge.safe_action_command?('dip my brush in my ceramic ink')
    assert LichAgentBridge.safe_action_command?('dip my brush in my cup')
    assert LichAgentBridge.safe_action_command?("draw odeir'cos rune")
    assert LichAgentBridge.safe_action_command?("draw ikar'fyn rune")
    assert LichAgentBridge.safe_action_command?("draw ag'loenar rune")
    assert LichAgentBridge.safe_action_command?("draw quiss'fyn rune")
    assert LichAgentBridge.safe_action_command?("draw ayan'eth rune")
    assert LichAgentBridge.safe_action_command?('wave my runestone at my scroll')
    assert LichAgentBridge.safe_action_command?('wave #16506792 at #15028257')
    assert LichAgentBridge.safe_action_command?('wave my polished obsidian runestone at my aged parchment')
    assert LichAgentBridge.safe_action_command?('prepare 714')
    assert LichAgentBridge.safe_action_command?('infuse my scroll')
    assert LichAgentBridge.safe_action_command?('infuse #15028257')
    assert LichAgentBridge.safe_action_command?('infuse my sheet of vellum')

    refute LichAgentBridge.safe_action_command?('draw unknown rune')
    refute LichAgentBridge.safe_action_command?("draw quiss'fyn2 rune")
    refute LichAgentBridge.safe_action_command?("draw odeir'cos rune; north")
    refute LichAgentBridge.safe_action_command?('wave my runestone at another scroll')
  end

  def test_shop_protocol_accepts_bare_order_and_buy
    assert LichAgentBridge.safe_action_command?('order')
    assert LichAgentBridge.safe_action_command?('buy')
  end

  def test_sequences_are_limited_to_local_inventory_inspection_and_crafting
    assert LichAgentBridge.safe_sequence_command?('get my runestone')
    assert LichAgentBridge.safe_sequence_command?('put my runestone in my robes')
    assert LichAgentBridge.safe_sequence_command?('read my scroll')
    assert LichAgentBridge.safe_sequence_command?('wave my stone at my scroll')
    assert LichAgentBridge.safe_sequence_command?('wave #16506792 at #15028257')
    assert LichAgentBridge.safe_sequence_command?('wave my polished obsidian runestone at my aged parchment')
    assert LichAgentBridge.safe_sequence_command?('prepare 714')
    assert LichAgentBridge.safe_sequence_command?('cast at #15028257')
    assert LichAgentBridge.safe_sequence_command?('infuse my scroll')
    assert LichAgentBridge.safe_sequence_command?('infuse #15028257')
    assert LichAgentBridge.safe_sequence_command?('infuse my sheet of vellum')

    refute LichAgentBridge.safe_sequence_command?('north')
    refute LichAgentBridge.safe_sequence_command?('attack troll')
    refute LichAgentBridge.safe_sequence_command?('say hello')
    refute LichAgentBridge.safe_sequence_command?('buy potion')
    refute LichAgentBridge.safe_sequence_command?('cast at my scroll')
    refute LichAgentBridge.safe_sequence_command?('cast at #15028257; north')
    refute LichAgentBridge.safe_sequence_command?('drop my runestone')
  end

  def test_owned_inventory_walk_excludes_external_container_contents
    satchel = FakeLabObject.new('10', 'satchel', 'black satchel', 'a black satchel', nil)
    stone = FakeLabObject.new('11', 'stone', 'smooth stone', 'a smooth stone', nil)
    external_gem = FakeLabObject.new('91', 'gem', 'blue sapphire', 'a blue sapphire', nil)
    GameObj.inventory = [satchel]
    GameObj.container_map = {
      '10' => [stone],
      '90' => [external_gem]
    }

    assert_equal %w[10 11], LichAgentBridge.owned_inventory_objects.map { |item| item.id.to_s }.sort
    snapshot = LichAgentBridge.owned_inventory_snapshot
    assert_equal '11', LichAgentBridge.owned_item_id('#11', snapshot)
    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.owned_item_id('#91', snapshot)
    end
  end

  def test_sequence_ownership_validates_carried_source_and_destination_without_rewriting
    robes = FakeLabObject.new('10', 'robes', 'black robes', 'some black robes', nil)
    runestone = FakeLabObject.new('11', 'runestone', 'chalcedony runestone', 'a chalcedony runestone', nil)
    GameObj.inventory = [robes, runestone]
    GameObj.container_map = { '90' => [] }

    assert_equal 'put my runestone in my robes', LichAgentBridge.validate_owned_sequence_command(
      'put my runestone in my robes'
    )
    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.validate_owned_sequence_command('put #11 in #90')
    end
    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.validate_owned_sequence_command('get my runestone from my iron chest')
    end
    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.validate_owned_sequence_command('put my runestone on the floor')
    end
  end

  def test_sequence_get_from_requires_the_actual_carried_parent
    robes = FakeLabObject.new('10', 'robes', 'black robes', 'some black robes', nil)
    satchel = FakeLabObject.new('11', 'satchel', 'black satchel', 'a black satchel', nil)
    stone = FakeLabObject.new('12', 'stone', 'smooth stone', 'a smooth stone', nil)
    GameObj.inventory = [robes, satchel]
    GameObj.container_map = { '11' => [stone] }

    assert_equal 'get my smooth stone from my black satchel', LichAgentBridge.validate_owned_sequence_command(
      'get my smooth stone from my black satchel'
    )
    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.validate_owned_sequence_command(
        'get my smooth stone from my black robes'
      )
    end
  end

  def test_sequence_ownership_rejects_ambiguous_nouns_but_accepts_exact_id
    first = FakeLabObject.new('10', 'ring', 'black ring', 'a black ring', nil)
    second = FakeLabObject.new('11', 'ring', 'black ring', 'a black ring', nil)
    GameObj.inventory = [first, second]

    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.validate_owned_sequence_command('wear my ring')
    end
    assert_equal 'wear #11', LichAgentBridge.validate_owned_sequence_command('wear #11')
    assert_raises(LichAgentBridge::SequenceOwnershipError) do
      LichAgentBridge.validate_owned_sequence_command('wear ring')
    end
  end

  def test_sequence_ownership_validates_every_explicit_crafting_operand
    potion = FakeLabObject.new('20', 'potion', 'ceramic potion', 'a ceramic potion', nil)
    stone = FakeLabObject.new('21', 'stone', 'smooth stone', 'a smooth stone', nil)
    brush = FakeLabObject.new('22', 'brush', 'sephwir brush', 'a sephwir brush', nil)
    ink = FakeLabObject.new('23', 'ink', 'ceramic ink', 'some ceramic ink', nil)
    scroll = FakeLabObject.new('24', 'scroll', 'aged scroll', 'an aged scroll', nil)
    GameObj.inventory = [potion, stone, brush, ink, scroll]

    commands = [
      'pour my potion on my stone',
      'dip my brush in my ceramic ink',
      'wave my stone at my scroll',
      'cast at #24',
      'infuse my scroll'
    ]
    commands.each do |command|
      assert_equal command, LichAgentBridge.validate_owned_sequence_command(command)
    end
  end

  def test_sequence_ownership_rejects_orphan_and_ambiguous_crafting_operands
    stone = FakeLabObject.new('20', 'stone', 'smooth stone', 'a smooth stone', nil)
    second_stone = FakeLabObject.new('21', 'stone', 'rough stone', 'a rough stone', nil)
    scroll = FakeLabObject.new('22', 'scroll', 'aged scroll', 'an aged scroll', nil)
    external_item = FakeLabObject.new('91', 'scroll', 'foreign scroll', 'a foreign scroll', nil)
    GameObj.inventory = [stone, second_stone, scroll]
    GameObj.container_map = { '90' => [external_item] }

    [
      'pour #91 on #20',
      'dip #20 in #91',
      'wave #20 at #91',
      'cast at #91',
      'infuse #91',
      'wave my stone at my scroll'
    ].each do |command|
      assert_raises(LichAgentBridge::SequenceOwnershipError, command) do
        LichAgentBridge.validate_owned_sequence_command(command)
      end
    end
  end

  def test_sequence_execution_rejects_external_destination_before_sending
    sent = []
    results = []
    original_send_sequence_command = LichAgentBridge.method(:send_sequence_command)
    LichAgentBridge.define_singleton_method(:send_sequence_command) do |command, **_options|
      sent << command
      true
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end
    GameObj.inventory = [
      FakeLabObject.new('11', 'runestone', 'runestone', 'a runestone', nil)
    ]
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      commands: ['put #11 in #90', 'read #11'],
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }

    LichAgentBridge.execute_action(action)

    assert_empty sent
    assert LichAgentBridge.instance_variable_get(:@actions_enabled)
    assert_equal 1, results.length
    assert_equal 'failed', results.first.fetch(:outcome)
    assert_match(/ownership check failed/, results.first.fetch(:detail))
  ensure
    LichAgentBridge.define_singleton_method(
      :send_sequence_command,
      original_send_sequence_command
    ) if defined?(original_send_sequence_command)
  end

  def test_sequence_execution_rejects_orphan_crafting_operand_without_disabling_actions
    sent = []
    results = []
    original_send_sequence_command = LichAgentBridge.method(:send_sequence_command)
    LichAgentBridge.define_singleton_method(:send_sequence_command) do |command, **_options|
      sent << command
      true
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end
    stone = FakeLabObject.new('11', 'stone', 'smooth stone', 'a smooth stone', nil)
    scroll = FakeLabObject.new('12', 'scroll', 'aged scroll', 'an aged scroll', nil)
    external_scroll = FakeLabObject.new('91', 'scroll', 'foreign scroll', 'a foreign scroll', nil)
    GameObj.inventory = [stone, scroll]
    GameObj.container_map = { '90' => [external_scroll] }
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      commands: ['wave #11 at #91', 'read #12'],
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }

    LichAgentBridge.execute_action(action)

    assert_empty sent
    assert LichAgentBridge.instance_variable_get(:@actions_enabled)
    assert_equal 1, results.length
    assert_equal 'failed', results.first.fetch(:outcome)
    assert_match(/ownership check failed/, results.first.fetch(:detail))
  ensure
    LichAgentBridge.define_singleton_method(
      :send_sequence_command,
      original_send_sequence_command
    ) if defined?(original_send_sequence_command)
  end

  def test_sequence_rechecks_ownership_after_each_completed_step
    sent = []
    results = []
    original_send_sequence_command = LichAgentBridge.method(:send_sequence_command)
    runestone = FakeLabObject.new('10', 'runestone', 'runestone', 'a runestone', nil)
    robes = FakeLabObject.new('11', 'robes', 'robes', 'some robes', nil)
    stone = FakeLabObject.new('12', 'stone', 'smooth stone', 'a smooth stone', nil)
    GameObj.inventory = [runestone, robes, stone]
    LichAgentBridge.define_singleton_method(:send_sequence_command) do |command, **_options|
      sent << command
      GameObj.inventory = [runestone, robes] if sent.length == 1
      true
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      commands: ['put my runestone in my robes', 'get my smooth stone'],
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }

    LichAgentBridge.execute_action(action)

    assert_equal ['put my runestone in my robes'], sent
    assert LichAgentBridge.instance_variable_get(:@actions_enabled)
    assert_equal 1, results.length
    assert_equal 'failed', results.first.fetch(:outcome)
    assert_match(/step 2 ownership check failed/, results.first.fetch(:detail))
  ensure
    LichAgentBridge.define_singleton_method(
      :send_sequence_command,
      original_send_sequence_command
    ) if defined?(original_send_sequence_command)
  end

  def test_scroll_infusion_sequence_acknowledges_actual_prepare_and_cast_responses
    assert LichAgentBridge.sequence_command_acknowledged?(
      "draw quiss'fyn rune",
      '',
      [
        "You carefully trace out the shape of the quiss'fyn rune with your " \
        'sephwir brush, forming a near-perfect depiction of it on the surface ' \
        'of your chalcedony runestone.'
      ]
    )
    assert LichAgentBridge.sequence_command_acknowledged?(
      'prepare 714',
      '',
      ['Your spell is ready.']
    )
    assert LichAgentBridge.sequence_command_acknowledged?(
      'cast at #15028257',
      '',
      ['You gesture at an aged scroll.']
    )
  end

  def test_explicit_sequence_executes_locally_in_order_and_reports_one_result
    sent = []
    results = []
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)
    original_send_sequence_command = LichAgentBridge.method(:send_sequence_command)
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false| force }
    LichAgentBridge.define_singleton_method(:send_sequence_command) do |command, **_options|
      sent << command
      true
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end
    GameObj.inventory = [
      FakeLabObject.new('10', 'runestone', 'runestone', 'a runestone', nil),
      FakeLabObject.new('11', 'robes', 'robes', 'some robes', nil),
      FakeLabObject.new('12', 'stone', 'smooth stone', 'a smooth stone', nil),
      FakeLabObject.new('13', 'scroll', 'scroll', 'a scroll', nil),
      FakeLabObject.new('14', 'satchel', 'satchel', 'a satchel', nil)
    ]
    commands = [
      'put my runestone in my robes',
      'get my smooth stone',
      'wave my stone at my scroll',
      'put my stone in my satchel',
      'get my runestone'
    ]
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      commands: commands,
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }

    LichAgentBridge.execute_action(action)

    assert_equal commands, sent
    assert_equal [:roundtime, :cast_roundtime] * commands.length, $roundtime_waits
    assert_equal 1, results.length
    assert_equal 'completed', results.first.fetch(:outcome)
    assert_equal 'sequence sent 5/5 commands', results.first.fetch(:detail)
    assert_empty LabInventory.capture_calls
  ensure
    LichAgentBridge.define_singleton_method(
      :send_sequence_command,
      original_send_sequence_command
    ) if defined?(original_send_sequence_command)
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
  end

  def test_explicit_sequence_stops_on_a_detected_game_rejection
    sent = []
    results = []
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)
    original_send_sequence_command = LichAgentBridge.method(:send_sequence_command)
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false| force }
    LichAgentBridge.define_singleton_method(:send_sequence_command) do |command, **_options|
      sent << command
      command == 'get my smooth stone' ? 'What were you referring to?' : 'Accepted.'
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end
    GameObj.inventory = [
      FakeLabObject.new('10', 'runestone', 'runestone', 'a runestone', nil),
      FakeLabObject.new('11', 'robes', 'robes', 'some robes', nil),
      FakeLabObject.new('12', 'stone', 'smooth stone', 'a smooth stone', nil),
      FakeLabObject.new('13', 'scroll', 'scroll', 'a scroll', nil)
    ]
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      commands: [
        'put my runestone in my robes',
        'get my smooth stone',
        'wave my stone at my scroll'
      ],
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }

    LichAgentBridge.execute_action(action)

    assert_equal ['put my runestone in my robes', 'get my smooth stone'], sent
    assert_equal 1, results.length
    assert_equal 'failed', results.first.fetch(:outcome)
    assert_match(/stopped at step 2/, results.first.fetch(:detail))
  ensure
    LichAgentBridge.define_singleton_method(
      :send_sequence_command,
      original_send_sequence_command
    ) if defined?(original_send_sequence_command)
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
  end

  def test_sequence_command_ignores_frontend_state_until_authoritative_prompt
    sent = Queue.new
    LichAgentBridge.define_singleton_method(:put) { |command| sent << command }

    command_thread = Thread.new do
      LichAgentBridge.send_sequence_command('wave #17663526 at #17663614', timeout: 1)
    end

    assert_equal 'wave #17663526 at #17663614', Timeout.timeout(1) { sent.pop }
    LichAgentBridge.capture('<despanaState version="1" payload="heartbeat"/>')
    sleep 0.02
    assert command_thread.alive?, 'frontend state traffic must not complete a game command'

    LichAgentBridge.capture("You wave a smooth stone at a shimmering scroll.\n<prompt time=\"1\">&gt;</prompt>")
    assert_equal true, Timeout.timeout(1) { command_thread.value }
  ensure
    command_thread&.kill
    LichAgentBridge.singleton_class.send(:remove_method, :put) if LichAgentBridge.singleton_methods(false).include?(:put)
  end

  def test_info_waiter_requires_own_complete_response_and_following_prompt
    sent = Queue.new
    LichAgentBridge.define_singleton_method(:put) { |command| sent << command }
    command_thread = Thread.new { LichAgentBridge.send_sequence_command('info', timeout: 1) }
    assert_equal 'info', Timeout.timeout(1) { sent.pop }
    LichAgentBridge.capture('<prompt time="1">&gt;</prompt>')
    assert command_thread.alive?
    lines = [
      'Name: Testmage Race: Dark Elf Profession: Sorcerer (not shown)',
      'Gender: Male Age: 40 Expr: 100000 Level: 90',
      *LichState::STAT_CODES.map { |name, code| "#{name.capitalize} (#{code}): 100 (25) ... 110 (30)" },
      'Mana: 300 Silver: 0'
    ]
    LichAgentBridge.capture(lines.join("\n"))
    assert command_thread.alive?, 'terminal text without following prompt is incomplete'
    LichAgentBridge.capture('<prompt time="2">&gt;</prompt>')
    assert_equal true, Timeout.timeout(1) { command_thread.value }
  ensure
    command_thread&.kill
    LichAgentBridge.singleton_class.send(:remove_method, :put) if LichAgentBridge.singleton_methods(false).include?(:put)
  end

  def test_recon_sequence_rechecks_generation_and_actions_before_commands
    original_sender = LichAgentBridge.method(:send_sequence_command)
    original_request = LichAgentBridge.method(:action_request)
    sent = []
    results = []
    LichAgentBridge.define_singleton_method(:send_sequence_command) { |command, **_options| sent << command; true }
    LichAgentBridge.define_singleton_method(:action_request) { |_path, payload| results << payload; {} }
    action = { action_id: 'recon-test', character: 'Testmage', commands: %w[info skills],
               generation: 'old-generation', expires_at: Time.now.to_f + 5, expected_room_id: '1000' }
    LichAgentBridge.execute_action(action)
    assert_empty sent
    assert_equal 'failed', results.last[:outcome]
    assert_includes results.last[:detail], 'generation changed'
    action[:generation] = LichAgentBridge.session_generation
    LichAgentBridge.define_singleton_method(:send_sequence_command) do |command, **_options|
      sent << command
      LichAgentBridge.instance_variable_set(:@actions_enabled, false)
      true
    end
    LichAgentBridge.execute_action(action)
    assert_equal ['info'], sent
    assert_equal 'failed', results.last[:outcome]
    assert_includes results.last[:detail], 'kill switch'
  ensure
    LichAgentBridge.define_singleton_method(:send_sequence_command, original_sender)
    LichAgentBridge.define_singleton_method(:action_request, original_request)
  end

  def test_sequence_command_ignores_unrelated_prompt_until_acknowledgment_and_following_prompt
    sent = Queue.new
    LichAgentBridge.define_singleton_method(:put) { |command| sent << command }

    command_thread = Thread.new do
      LichAgentBridge.send_sequence_command('put #17663613 in #17663565', timeout: 1)
    end

    assert_equal 'put #17663613 in #17663565', Timeout.timeout(1) { sent.pop }
    LichAgentBridge.capture('<prompt time="1">&gt;</prompt>')
    sleep 0.02
    assert command_thread.alive?, 'an unrelated prompt must not complete an unacknowledged command'

    LichAgentBridge.capture('You roll the pure white scroll tightly and slip it into your satchel.')
    sleep 0.02
    assert command_thread.alive?, 'the command must wait for the prompt following its acknowledgment'

    LichAgentBridge.capture('<prompt time="2">&gt;</prompt>')
    assert_equal true, Timeout.timeout(1) { command_thread.value }
  ensure
    command_thread&.kill
    LichAgentBridge.singleton_class.send(:remove_method, :put) if LichAgentBridge.singleton_methods(false).include?(:put)
  end

  def test_eherbs_heal_requires_staunching_and_reports_script_completion
    starts = Queue.new
    results = Queue.new
    running = false
    original_running = Script.method(:running)
    original_spell_lookup = Spell.method(:[]) if Spell.respond_to?(:[])
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)

    Spell.define_singleton_method(:[]) { |_number| Struct.new(:active?).new(true) }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false| force }
    Script.define_singleton_method(:running?) { |name| name == 'eherbs' && running }
    Script.define_singleton_method(:running) { running ? [Struct.new(:name).new('eherbs')] : [] }
    Script.define_singleton_method(:run) do |name, arguments, options|
      running = true
      starts << [name, arguments, options]
      Thread.new do
        sleep 0.2
        running = false
      end
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end

    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      command: 'eherbs heal',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }
    LichAgentBridge.execute_action(action)

    assert_equal [
      'eherbs',
      '--buy=off --deposit=off --skipscars=on',
      { quiet: true }
    ], Timeout.timeout(1) { starts.pop }
    result = Timeout.timeout(1) { results.pop }
    assert_equal 'completed', result.fetch(:outcome), result.inspect
  ensure
    Script.singleton_class.send(:remove_method, :running?) if Script.respond_to?(:running?)
    Script.singleton_class.send(:remove_method, :run) if Script.respond_to?(:run)
    Script.define_singleton_method(:running, original_running) if defined?(original_running)
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
    if defined?(original_spell_lookup) && original_spell_lookup
      Spell.define_singleton_method(:[], original_spell_lookup)
    elsif Spell.respond_to?(:[])
      Spell.singleton_class.send(:remove_method, :[])
    end
  end

  def test_lab_hunt_return_requests_safe_handoff_without_killing_bigshot
    results = Queue.new
    kills = []
    original_running = Script.method(:running)
    original_running_predicate = Script.method(:running?) if Script.respond_to?(:running?)
    original_kill = Script.method(:kill) if Script.respond_to?(:kill)
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)
    original_character_name = LichAgentBridge.method(:character_name)

    Script.define_singleton_method(:running?) { |name| %w[lab-test-hunt bigshot].include?(name) }
    Script.define_singleton_method(:running) do
      %w[lab-test-hunt bigshot].map { |name| Struct.new(:name).new(name) }
    end
    Script.define_singleton_method(:kill) { |name| kills << name }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false| force }
    LichAgentBridge.define_singleton_method(:character_name) { 'Testknight' }
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end
    $lab_hunt_return_requested = false

    action = {
      action_id: '1234567890abcdef',
      character: 'Testknight',
      command: 'lab-test-hunt return',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }
    LichAgentBridge.execute_action(action)

    result = Timeout.timeout(1) { results.pop }
    assert $lab_hunt_return_requested
    assert_equal 'completed', result.fetch(:outcome), result.inspect
    assert_empty kills
  ensure
    $lab_hunt_return_requested = false
    Script.define_singleton_method(:running, original_running) if defined?(original_running)
    if defined?(original_running_predicate) && original_running_predicate
      Script.define_singleton_method(:running?, original_running_predicate)
    elsif Script.respond_to?(:running?)
      Script.singleton_class.send(:remove_method, :running?)
    end
    if defined?(original_kill) && original_kill
      Script.define_singleton_method(:kill, original_kill)
    elsif Script.respond_to?(:kill)
      Script.singleton_class.send(:remove_method, :kill)
    end
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
    LichAgentBridge.define_singleton_method(:character_name, original_character_name) if defined?(original_character_name)
  end

  def test_bigshot_start_acknowledges_launch_without_waiting_for_script_exit
    entered = Queue.new
    release = Queue.new
    results = Queue.new
    snapshots = Queue.new
    running_script = Struct.new(:name).new('bigshot')
    running = false
    original_running = Script.method(:running)
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)

    Script.define_singleton_method(:running?) { |name| name == 'bigshot' && running }
    Script.define_singleton_method(:running) { running ? [running_script] : [] }
    Script.define_singleton_method(:run) do |name, _arguments, _options|
      running = true
      entered << name
      release.pop
      running = false
    end
    LichAgentBridge.define_singleton_method(:publish_snapshot) do |force: false|
      snapshots << snapshot_state if force
      true
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end

    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      command: 'bigshot start',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }
    execution = Thread.new { LichAgentBridge.execute_action(action) }

    assert_equal 'bigshot', Timeout.timeout(1) { entered.pop }
    result = Timeout.timeout(1) { results.pop }
    snapshot = Timeout.timeout(1) { snapshots.pop }
    assert_equal 'completed', result.fetch(:outcome)
    assert_includes snapshot.fetch(:scripts), 'bigshot'
    assert execution.join(1), 'execute_action waited for Bigshot to exit'
  ensure
    release << true if defined?(release)
    execution&.join(1)
    Script.singleton_class.send(:remove_method, :running?) if Script.respond_to?(:running?)
    Script.singleton_class.send(:remove_method, :run) if Script.respond_to?(:run)
    Script.define_singleton_method(:running, original_running) if defined?(original_running)
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
  end

  def test_lab_engage_acknowledges_launch_without_waiting_for_target_death
    entered = Queue.new
    results = Queue.new
    running_script = Struct.new(:name).new('lab-test-engage')
    running = false
    original_running = Script.method(:running)
    original_running_query = Script.method(:running?) if Script.respond_to?(:running?)
    original_start = Script.method(:start) if Script.respond_to?(:start)
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)
    original_action_request = LichAgentBridge.method(:action_request)
    original_monitor = LichAgentBridge.method(:monitor_controller)
    original_character_name = LichAgentBridge.method(:character_name)

    Script.define_singleton_method(:running?) { |name| name == 'lab-test-engage' && running }
    Script.define_singleton_method(:running) { running ? [running_script] : [] }
    Script.define_singleton_method(:start) do |name, arguments|
      running = true
      entered << [name, arguments]
      running_script
    end
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false| force }
    LichAgentBridge.define_singleton_method(:character_name) { 'Testknight' }
    LichAgentBridge.define_singleton_method(:monitor_controller) do |_controller, execution_id:|
      execution_id
    end
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end

    action = {
      action_id: '1234567890abcdef',
      character: 'Testknight',
      command: 'lab-test-engage test-living #185638160 loot',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }
    execution = Thread.new { LichAgentBridge.execute_action(action) }

    launch = Timeout.timeout(1) { entered.pop }
    result = Timeout.timeout(1) { results.pop }
    assert_equal ['lab-test-engage', 'test-living #185638160 loot'], launch
    assert_equal 'completed', result.fetch(:outcome)
    assert execution.join(1), 'execute_action waited for lab-test-engage to exit'
    running = false
  ensure
    execution&.join(1)
    if defined?(original_running_query) && original_running_query
      Script.define_singleton_method(:running?, original_running_query)
    elsif Script.respond_to?(:running?)
      Script.singleton_class.send(:remove_method, :running?)
    end
    if defined?(original_start) && original_start
      Script.define_singleton_method(:start, original_start)
    elsif Script.respond_to?(:start)
      Script.singleton_class.send(:remove_method, :start)
    end
    Script.define_singleton_method(:running, original_running) if defined?(original_running)
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
    LichAgentBridge.define_singleton_method(
      :action_request,
      original_action_request
    ) if defined?(original_action_request)
    LichAgentBridge.define_singleton_method(
      :monitor_controller,
      original_monitor
    ) if defined?(original_monitor)
    LichAgentBridge.define_singleton_method(:character_name, original_character_name) if defined?(original_character_name)
  end

  def test_lab_engage_failed_launch_clears_starting_status
    results = Queue.new
    original_launch = LichAgentBridge.method(:launch_controller)
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)
    original_running_query = Script.method(:running?) if Script.respond_to?(:running?)
    original_character_name = LichAgentBridge.method(:character_name)

    Script.define_singleton_method(:running?) { |_name| false }
    LichAgentBridge.define_singleton_method(:launch_controller) { |_controller, _controller_action, _arguments| false }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |force: false| force }
    LichAgentBridge.define_singleton_method(:character_name) { 'Testknight' }
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      results << payload if path == '/v1/actions/result'
      {}
    end

    action = {
      action_id: '1234567890abcdef',
      character: 'Testknight',
      command: 'lab-test-engage test-living #185638160 loot',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }

    LichAgentBridge.execute_action(action)

    result = Timeout.timeout(1) { results.pop }
    assert_equal 'failed', result.fetch(:outcome)
    assert_equal 'lab-test-engage did not start', result.fetch(:detail)
    assert_equal(
      'failed:1234567890abcdef',
      LichAgentBridge.instance_variable_get(:@script_status).fetch('lab-test-engage')
    )
  ensure
    if defined?(original_running_query) && original_running_query
      Script.define_singleton_method(:running?, original_running_query)
    elsif Script.respond_to?(:running?)
      Script.singleton_class.send(:remove_method, :running?)
    end
    LichAgentBridge.define_singleton_method(:launch_controller, original_launch) if defined?(original_launch)
    LichAgentBridge.define_singleton_method(
      :publish_snapshot,
      original_publish_snapshot
    ) if defined?(original_publish_snapshot)
    LichAgentBridge.define_singleton_method(:character_name, original_character_name) if defined?(original_character_name)
  end

  def test_generic_controller_monitor_publishes_post_state_before_attributed_result
    publications = Queue.new
    running = true
    original_running_query = Script.method(:running?) if Script.respond_to?(:running?)
    original_publish_snapshot = LichAgentBridge.method(:publish_snapshot)
    original_publish_event = LichAgentBridge.method(:publish_event)
    controller = LichAgentBridge::CONTROLLER_REGISTRY.controller('hunt')
    $lab_hunt_result = {
      ok: true,
      code: :returned,
      message: 'safe handoff complete',
      details: { room_id: 1000 }
    }

    Script.define_singleton_method(:running?) { |name| name == 'lab-test-hunt' && running }
    LichAgentBridge.define_singleton_method(:publish_snapshot) do |force: false|
      publications << [:snapshot, force]
      true
    end
    LichAgentBridge.define_singleton_method(:publish_event) do |kind, summary, data|
      publications << [:event, kind, summary, data]
      true
    end

    monitor = LichAgentBridge.monitor_controller(
      controller,
      execution_id: '1234567890abcdef'
    )
    running = false
    assert monitor.join(1), 'controller monitor did not observe script exit'

    snapshot = Timeout.timeout(1) { publications.pop }
    event = Timeout.timeout(1) { publications.pop }
    assert_equal [:snapshot, true], snapshot
    assert_equal 'controller_result', event[1]
    assert_equal 'hunt', event[3].fetch(:controller)
    assert_equal '1234567890abcdef', event[3].fetch(:action_id)
    assert_equal true, event[3].fetch(:ok)
    assert_equal 'returned', event[3].fetch(:code)
    assert_equal({ 'room_id' => 1000 }, event[3].fetch(:details))
  ensure
    $lab_hunt_result = nil
    if defined?(original_running_query) && original_running_query
      Script.define_singleton_method(:running?, original_running_query)
    elsif Script.respond_to?(:running?)
      Script.singleton_class.send(:remove_method, :running?)
    end
    LichAgentBridge.define_singleton_method(:publish_snapshot, original_publish_snapshot) if defined?(original_publish_snapshot)
    LichAgentBridge.define_singleton_method(:publish_event, original_publish_event) if defined?(original_publish_event)
  end

  def test_miracle_teleport_is_narrowly_allowlisted
    assert LichAgentBridge.safe_action_command?('beseech teleport')
    refute LichAgentBridge.safe_action_command?('beseech')
    refute LichAgentBridge.safe_action_command?('beseech anything-else')
  end

  def test_running_bridge_reregisters_after_sidecar_loses_state
    assert LichAgentBridge.reconcile_action_registration
    assert_equal 1, @requests.length

    # A fresh sidecar has forgotten the prior registration. Advancing the
    # client's reconciliation clock must make it register again without a
    # Lich script restart.
    LichAgentBridge.instance_variable_set(:@last_action_registration_at, Time.at(0))

    assert LichAgentBridge.reconcile_action_registration
    assert_equal 2, @requests.length
    assert_equal [
      '/v1/actions/control',
      { character: 'Testmage', generation: LichAgentBridge.session_generation, enabled: true }
    ], @requests.last
  end

  def test_reconciliation_is_periodic
    LichAgentBridge.instance_variable_set(:@last_action_registration_at, Time.now)

    refute LichAgentBridge.reconcile_action_registration
    assert_empty @requests
  end

  def test_explicit_local_kill_switch_prevents_reregistration
    LichAgentBridge.instance_variable_set(:@actions_enabled, false)

    refute LichAgentBridge.reconcile_action_registration
    assert_empty @requests
  end

  def test_rejected_snapshot_generation_stops_action_registration
    LichAgentBridge.instance_variable_set(:@snapshot_generation_rejected, true)

    refute LichAgentBridge.reconcile_action_registration
    refute LichAgentBridge.instance_variable_get(:@actions_enabled)
    assert_empty @requests
  end

  def test_failed_initial_enable_stays_requested_and_recovers_later
    LichAgentBridge.define_singleton_method(:action_request) do |_path, _payload|
      raise Errno::ECONNREFUSED, 'sidecar is starting'
    end

    LichAgentBridge.set_actions(true)

    assert LichAgentBridge.instance_variable_get(:@actions_enabled)

    requests = @requests
    LichAgentBridge.define_singleton_method(:action_request) do |path, payload|
      requests << [path, payload]
      { character: payload.fetch(:character), enabled: payload.fetch(:enabled) }
    end
    LichAgentBridge.instance_variable_set(:@last_action_registration_at, Time.at(0))

    assert LichAgentBridge.reconcile_action_registration
    assert_equal 1, @requests.length
  end

  def test_failed_disable_remains_locally_off
    LichAgentBridge.define_singleton_method(:action_request) do |_path, _payload|
      raise Errno::ECONNREFUSED, 'sidecar disappeared'
    end

    LichAgentBridge.set_actions(false)

    refute LichAgentBridge.instance_variable_get(:@actions_enabled)
  end
end
