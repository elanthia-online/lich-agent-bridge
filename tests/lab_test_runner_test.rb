require 'minitest/autorun'
require 'tmpdir'
require 'digest'
require 'json'
require_relative '../lich/lab-test-runner'

class LabTestRunnerTest < Minitest::Test
  class Child
    attr_reader :kills
    def initialize(error: nil, blocked: false, cleanup: true)
      @error, @blocked, @cleanup, @kills = error, blocked, cleanup, 0
    end
    def join(seconds)
      sleep [seconds, 0.001].min if seconds.positive? && @blocked
      @blocked ? nil : self
    end
    def kill_sync(timeout:)
      @kills += 1
      @blocked = false if @cleanup
      join(0)
    end
    def completed_successfully? = !@error && @kills.zero?
    def exit_error = @error
  end

  def setup
    @root = Dir.mktmpdir('lab-test-runner-')
    @manifest = {
      'version' => 1, 'id' => 'probe', 'script' => 'probe', 'files' => ['probe.lic'],
      'cases' => [{'id' => 'normal', 'args' => ['normal'],
                   'assertions' => [{'field' => 'room_id', 'op' => 'unchanged'}]}],
      'limits' => {'case_seconds' => 0.03, 'run_seconds' => 0.1, 'cleanup_seconds' => 0.03}
    }
    File.write(File.join(@root, 'probe.lic'), 'nil')
    %w[lab-test-runner.rb lab-test-runner.lic].each do |name|
      File.write(File.join(@root, name), File.read(File.expand_path("../lich/#{name}", __dir__)))
    end
    @state = {'character' => 'Testmage', 'generation' => 'generation-1', 'room_id' => '1000',
              'dead' => false, 'stunned' => false, 'health' => 100}
    @child = Child.new
    @starts = []
    @context = {'action_id' => 'action-1', 'character' => 'Testmage', 'generation' => 'generation-1',
                'room_id' => '1000', 'case_id' => 'normal', 'suite_id' => 'probe'}
  end

  def teardown
    FileUtils.remove_entry(@root)
  end

  def suite
    File.write(File.join(@root, 'suite.json'), JSON.generate(@manifest))
    files = %w[suite.json probe.lic lab-test-runner.lic lab-test-runner.rb].to_h do |name|
      [name, Digest::SHA256.file(File.join(@root, name)).hexdigest]
    end
    @context['revision'] = files.fetch('suite.json')
    LabTestRunner::Suite.new(root: @root, metadata: {'manifest' => 'suite.json', 'files' => files},
                             suite_id: 'probe', revision: @context['revision'], case_id: @context['case_id'],
                             resolve: ->(name) { "#{name}.lic" })
  end

  def runner(selected = suite, control: -> { true }, snapshot: -> { @state.dup })
    LabTestRunner::Runner.new(suite: selected, context: @context, snapshot: snapshot,
                             control: control, report_directory: File.join(@root, 'reports'),
                             start_child: ->(name, args) { @starts << [name, args]; @child })
  end

  def test_original_suite_runs_fixed_arguments_assertions_and_private_exclusive_report
    result = runner.run
    assert_equal [['probe', 'normal']], @starts
    assert result[:ok]
    assert_equal 'passed', result[:details][:status]
    assert result[:details][:cleanup_complete]
    report = JSON.parse(File.read(result[:details][:report_path]))
    assert_equal Digest::SHA256.file(File.join(@root, 'probe.lic')).hexdigest, report.fetch('files').fetch('probe.lic')
    assert_equal true, report['cases'][0]['assertions'][0]['passed']
    assert_equal '1000', report['cases'][0]['assertions'][0]['actual']
    assert_equal 0o600, File.stat(result[:details][:report_path]).mode & 0o777
    assert_equal 0o700, File.stat(File.dirname(result[:details][:report_path])).mode & 0o777
    second = runner.run
    refute_equal result[:details][:report_path], second[:details][:report_path]
  end

  def test_digest_change_rejected_before_start
    selected = suite
    File.write(File.join(@root, 'probe.lic'), 'changed')
    assert_raises(LabTestRunner::Invalid) { runner(selected).run }
    assert_empty @starts
  end

  def test_shadowed_and_prefix_resolved_target_rejected
    selected = suite
    Dir.mkdir(File.join(@root, 'custom'))
    File.write(File.join(@root, 'custom', 'probe.lic'), 'nil')
    assert_raises(LabTestRunner::Invalid) { selected.verify! }
    File.delete(File.join(@root, 'custom', 'probe.lic'))
    selected.instance_variable_set(:@resolve, ->(name) { name == 'probe' ? 'probe-other.lic' : "#{name}.lic" })
    assert_raises(LabTestRunner::Invalid) { selected.verify! }
  end

  def test_invalid_parameters_assertions_paths_and_limits_rejected
    mutations = [
      -> { @manifest['version'] = 1.0 },
      -> { @manifest['cases'][0]['args'] = ['normal;kill'] },
      -> { @manifest['cases'][0]['assertions'][0]['op'] = 'eval' },
      -> { @manifest['files'] = ['../probe.lic'] },
      -> { @manifest['limits']['run_seconds'] = 21 },
      -> { @manifest['cases'][0]['assertions'] = [] }
    ]
    original = JSON.generate(@manifest)
    mutations.each do |mutation|
      @manifest = JSON.parse(original)
      mutation.call
      assert_raises(LabTestRunner::Invalid) { suite }
    end
  end

  def test_approval_delay_reserves_cleanup_and_never_launches_after_work_deadline
    @context['expires_at'] = Time.now.to_f + 0.01
    result = runner.run
    refute result[:ok]
    assert_empty @starts
    assert result[:details][:cleanup_complete]
  end

  def test_report_directory_failure_is_detected_before_launch
    selected = suite
    File.write(File.join(@root, 'reports'), 'existing protected data')
    assert_raises(LabTestRunner::Invalid) { runner(selected).run }
    assert_empty @starts
    assert_equal 'existing protected data', File.read(File.join(@root, 'reports'))
  end

  def test_verification_consuming_work_budget_prevents_child_launch
    selected = suite
    test_runner = runner(selected)
    clock = [0.0]
    test_runner.define_singleton_method(:now) { clock.first }
    verify = selected.method(:verify!)
    verifications = 0
    selected.define_singleton_method(:verify!) do
      verify.call
      verifications += 1
      clock[0] = 1.0 if verifications == 2
    end

    result = test_runner.run
    refute result[:ok]
    assert_empty @starts
    assert result[:details][:cleanup_complete]
    report = JSON.parse(File.read(result[:details][:report_path]))
    assert_includes report['cases'][0]['reason'], 'run deadline reached'
  end

  def test_state_changed_during_verification_prevents_child_launch
    selected = suite
    verify = selected.method(:verify!)
    verifications = 0
    state = @state
    selected.define_singleton_method(:verify!) do
      verify.call
      verifications += 1
      state['generation'] = 'replacement' if verifications == 2
    end

    result = runner(selected).run
    refute result[:ok]
    assert_empty @starts
    assert result[:details][:cleanup_complete]
  end

  def test_child_already_complete_after_delayed_start_is_not_a_pass
    test_runner = runner
    clock = [0.0]
    test_runner.define_singleton_method(:now) { clock.first }
    test_runner.instance_variable_set(:@start_child, lambda do |name, args|
      @starts << [name, args]
      clock[0] = 1.0
      @child
    end)

    result = test_runner.run
    assert_equal 1, @starts.length
    refute result[:ok]
    assert result[:details][:cleanup_complete]
    report = JSON.parse(File.read(result[:details][:report_path]))
    assert_includes report['cases'][0]['reason'], 'case deadline reached'
  end

  def test_unknown_observation_is_inconclusive
    @manifest['cases'][0]['assertions'] = [{'field' => 'mana', 'op' => 'equals', 'value' => 0}]
    result = runner.run
    refute result[:ok]
    assert_equal 'inconclusive', result[:details][:status]
  end

  def test_failed_assertion_and_script_error_are_failures
    @manifest['cases'][0]['assertions'] = [{'field' => 'health', 'op' => 'equals', 'value' => 99}]
    assert_equal 'failed', runner.run[:details][:status]
    @child = Child.new(error: RuntimeError.new('synthetic failure'))
    result = runner.run
    assert_equal 'failed', result[:details][:status]
    assert_includes File.read(result[:details][:report_path]), 'synthetic failure'
  end

  def test_timeout_stops_exact_instance_and_reports_incomplete_cleanup
    @child = Child.new(blocked: true, cleanup: false)
    test_runner = runner
    result = test_runner.run
    assert_equal 1, @child.kills
    refute result[:details][:cleanup_complete]
    refute test_runner.cleanup_complete?
    @child.instance_variable_set(:@blocked, false)
    assert test_runner.cleanup_complete?
  end

  def test_revoked_control_and_replaced_identity_launch_nothing
    result = runner(control: -> { false }).run
    refute result[:ok]
    assert_empty @starts
    @state['generation'] = 'replacement'
    result = runner.run
    refute result[:ok]
    assert_empty @starts
  end

  def test_periodic_local_state_change_stops_exact_child_and_skips_following_cases
    @context['case_id'] = 'all'
    @manifest['cases'] << {'id' => 'later', 'args' => [], 'assertions' => [{'field' => 'room_id', 'op' => 'unchanged'}]}
    @child = Child.new(blocked: true)
    checks = 0
    snapshot = -> { checks += 1; @state.merge('stunned' => checks > 2) }
    result = runner(snapshot: snapshot).run
    assert_equal 1, @starts.length
    assert_equal 1, @child.kills
    report = JSON.parse(File.read(result[:details][:report_path]))
    assert_equal 'skipped', report['cases'].last['status']
  end
end
