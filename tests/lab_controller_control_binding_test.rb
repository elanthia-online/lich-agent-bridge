require 'minitest/autorun'
require_relative '../lich/lab-controller-registry'
require_relative '../lich/lab-controller-controls'

# Synthetic adapter tests. Runtime is a bounded fake of QuickRun#request; these
# do not establish native Bigshot, socket, or live owner-thread compatibility.
class LabControllerControlBindingTest < Minitest::Test
  class Runtime
    attr_reader :requests
    def initialize = (@requests = [])
    def status = { state: 'engaged' }.freeze
    def request(action, valid: nil)
      @requests << [action, valid]
      { accepted: true, status: status }
    end
  end

  def setup
    @registry = LabControllerRegistry.load(File.expand_path('fixtures/controller-controls.json', __dir__))
    @controller = @registry.controller('quick')
    @now, @available, @reads = 1000.0, true, 0
    @launch = { action_id: '0123456789abcdef', character: 'Testmage', generation: 'session-1' }
    @instance, @runtime, @authority = Object.new, Runtime.new, {}
    @binding = LabControllerControls::Binding.new(
      controller: @controller, launch: @launch, instance: @instance,
      available: ->(_action) { @available }, clock: -> { @now },
      authority: ->(id) { @reads += 1; @authority[id] }
    )
    @binding.bind(@instance, @runtime)
  end

  def enqueue(verb = 'hold', id: 'aaaaaaaaaaaaaaaa', run_id: @launch[:action_id], **overrides)
    command = "lab-test-quick #{verb} #{run_id}"
    action = { action_id: id, character: 'Testmage', generation: 'session-1',
               command: command, expected_room_id: '1000', expires_at: 1002.0 }.merge(overrides)
    @authority[id] = action.merge(status: 'dispatched', stop_requested: false)
    @binding.request(action: action, match: @registry.match(command))
  end

  def test_admission_reports_queued_not_applied_and_predicate_never_reads_transport
    result = enqueue
    assert_equal 'control_queued', result[:code]
    assert_nil result[:details][:applied]
    assert_equal @launch[:action_id], result[:details][:run_id]
    assert_equal 'aaaaaaaaaaaaaaaa', result[:details][:control_action_id]
    reads = @reads
    assert_equal true, @runtime.requests.first.last.call
    assert_equal reads, @reads
  end

  def test_status_returns_observation_not_terminal_completion
    result = enqueue('status')
    assert_equal 'control_status', result[:code]
    assert_equal({ state: 'engaged' }, result[:details][:status])
    assert_nil result[:details][:applied]
  end

  def test_launch_lifetime_uses_operation_deadline_not_spent_dispatch_ttl
    action = { action_id: 'aaaaaaaaaaaaaaaa', expires_at: 999.0, controller_deadline: 1005.0 }
    observed = action.merge(status: 'completed', stop_requested: false)
    lease = LabControllerControls::Lease.new(action: action, deadline: 1005.0,
      available: ->(_) { true }, authority: ->(_) { observed }, clock: -> { @now })
    assert_equal true, lease.refresh
    @now = 1005.0
    assert_equal false, lease.refresh
    assert_equal true, lease.expired?
  end

  def test_opt_in_example_matches_direct_bigshot_trial_without_enabling_public_registry
    registry = LabControllerRegistry.load(File.expand_path('../examples/controllers/bigshot-quick-trial.json', __dir__))
    match = registry.match('bigshot quick trial probe-sequence --target 12345 --area profile')
    assert_equal 'bigshot', match.controller.script
    assert_equal 'quick trial probe-sequence --target 12345 --area profile', match.script_args
    assert_equal 'quick_refuge', match.controller.safe_handoff['kind']
    assert_equal ['bigshot'], match.controller.control_owner_scripts
    refute registry.match('bigshot quick trial unreviewed --target 12345 --area profile')
    refute registry.match('bigshot quick trial probe-sequence --target 12345')
    public_registry = LabControllerRegistry.load(File.expand_path('../lich/lab-controllers.json', __dir__))
    refute public_registry.match('bigshot quick trial probe-sequence --target 12345 --area profile')
  end

  def test_expired_and_stale_cached_authority_prevent_queued_application
    enqueue
    valid = @runtime.requests.first.last
    @now += 0.3
    assert_equal false, valid.call
    @binding.refresh
    assert_equal true, valid.call
    @now = 1002.0
    assert_equal false, valid.call
  end

  def test_backward_clock_and_unknown_local_availability_fail_closed
    enqueue
    @now -= 1
    assert_equal false, @runtime.requests.first.last.call
    @now = 1000.0
    @available = nil
    assert_equal false, @runtime.requests.first.last.call
  end

  def test_observed_revocation_latches_and_does_not_revoke_another_control
    enqueue
    enqueue('resume', id: 'bbbbbbbbbbbbbbbb')
    @authority['aaaaaaaaaaaaaaaa'][:stop_requested] = true
    @binding.refresh
    assert_equal false, @runtime.requests[0].last.call
    assert_equal true, @runtime.requests[1].last.call
    @authority['aaaaaaaaaaaaaaaa'][:stop_requested] = false
    @binding.refresh
    assert_equal false, @runtime.requests[0].last.call
  end

  def test_missing_mismatched_or_unavailable_authority_cannot_enable_controls
    enqueue
    @authority['aaaaaaaaaaaaaaaa'][:generation] = 'replaced-session'
    @binding.refresh
    assert_equal false, @runtime.requests.first.last.call
    @available = false
    assert_raises(LabControllerControls::Invalid) { enqueue('resume', id: 'bbbbbbbbbbbbbbbb') }
    assert_equal 1, @runtime.requests.length
  end

  def test_closed_or_replaced_instance_never_receives_old_controls
    assert_raises(LabControllerControls::Invalid) { @binding.bind(Object.new, @runtime) }
    assert_raises(LabControllerControls::Invalid) { @binding.bind(@instance, @runtime) }
    enqueue
    @binding.close
    assert_equal false, @runtime.requests.first.last.call
    assert_raises(LabControllerControls::Invalid) { enqueue('resume') }
  end

  def test_character_generation_and_launch_token_must_match
    [{ character: 'Othermage' }, { generation: 'old-session' }, { run_id: 'ffffffffffffffff' }].each do |overrides|
      assert_raises(LabControllerControls::Invalid) { enqueue(**overrides) }
    end
    assert_empty @runtime.requests
  end

  def test_queue_is_bounded_and_expired_entries_release_capacity
    32.times { |index| enqueue(id: format('%016x', index)) }
    assert_raises(LabControllerControls::Invalid) { enqueue }
    @now = 1002.0
    @binding.refresh
    result = enqueue(expires_at: 1003.0)
    assert_equal 'control_queued', result[:code]
  end

  def test_rejected_runtime_drops_reserved_lease_and_revokes_any_captured_predicate
    @runtime.define_singleton_method(:request) do |action, valid:|
      @requests << [action, valid]
      { accepted: false }
    end
    33.times do |index|
      error = assert_raises(LabControllerControls::Invalid) { enqueue(id: format('%016x', index)) }
      assert_match(/runtime rejected/, error.message)
    end
    assert_equal false, @runtime.requests.first.last.call
  end
end
