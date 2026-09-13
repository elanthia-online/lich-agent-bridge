# frozen_string_literal: true

require 'minitest/autorun'
require_relative '../lich/lab-paired-trial'
require_relative '../lich/lab-controller-registry'
require_relative '../lich/lab-controller-controls'

# Requires a reviewed Hunter checkout; never loads Lich/game I/O. Native Guard
# and Immutable are tested through the actual adapter interface, not doubles.
module EO
  module Engine; end
end
require File.join(ENV['LAB_TEST_HUNTER_ROOT'], 'scripts/eohunter/controller') if ENV['LAB_TEST_HUNTER_ROOT']

class LabPairedTrialTest < Minitest::Test
  def setup
    skip 'set LAB_TEST_HUNTER_ROOT to a reviewed Hunter checkout' unless ENV['LAB_TEST_HUNTER_ROOT']
    @now, @authorized = 100.0, true
    @identity = { character: 'Testmage', session: 'session-a', run_id: 'run-a' }
    @peer = { character: 'Testfriend', session: 'session-b', run_id: 'run-b' }
    @observed = { connected: true, alive: true, owner: true, stable: true, ready: true,
      standing: true, room_id: 1000, session: 'session-a', hands: ['123', nil],
      owner_released: true, children_released: true }
    @owner = Object.new
    @owner.define_singleton_method(:execution_sleep) { |_seconds| raise 'unexpected startup sleep' }
    @launch = EO::Engine::Controller::Launch.new(work_deadline: 150.0, cleanup_deadline: 160.0,
      refuge_room: 1000, return_deadline: 200.0)
    @runtime = build
  end

  def build
    LabPairedTrial::Runtime.new(native: EO::Engine::Controller, owner: @owner, launch: @launch,
      identity: @identity, snapshot: -> { @observed.dup }, clock: -> { @now })
  end

  def activate
    assert @runtime.activate_supervised(valid: -> { @authorized })
    assert @runtime.await_supervisor!
  end

  def local_receipt
    { identity: @identity, safe: true, refuge_room: 1000, hands: ['123', nil], reason: 'kill_limit' }
  end

  def shared
    { complete: true, success: true, reason: 'kill_limit', target_id: '999',
      receipts: [local_receipt, { identity: @peer, safe: true, refuge_room: 1000, hands: ['456', nil], reason: 'kill_limit' }] }
  end

  def finish(local: local_receipt, result: shared, cleaned: true)
    @runtime.bind_pair!([@identity, @peer])
    @runtime.finish(local: local, shared: result, cleaned: cleaned)
  end

  def test_no_authority_before_single_activation
    refute @runtime.permitted?
    # Checking an unactivated native guard is a denial, not activation.
    activate
    refute @runtime.activate_supervised(valid: -> { true })
    assert @runtime.permitted?
    assert @runtime.status.frozen?
    assert @runtime.status[:refuge].frozen?
  end

  def test_start_requires_known_safe_original_state
    %i[connected alive owner stable ready standing].each do |key|
      @observed[key] = false
      assert_raises(LabPairedTrial::Invalid) { build }
      @observed[key] = true
    end
    @observed[:hands] = [nil]
    assert_raises(LabPairedTrial::Invalid) { build }
  end

  def test_malformed_local_identity_is_refused
    @identity = @identity.merge(run_id: '')
    assert_raises(LabPairedTrial::Invalid) { build }
  end

  def test_final_observation_failure_closes_without_inventing_recovery
    activate
    @runtime.instance_variable_set(:@snapshot, -> { raise IOError, 'reader unavailable' })
    value = finish
    assert_equal :stopped, value[:state]
    refute value[:refuge][:returned]
    assert_same value, @runtime.close
  end

  def test_native_runtime_through_real_lab_binding
    activate
    registry = LabControllerRegistry.load(File.expand_path('fixtures/controller-controls.json', __dir__))
    launch = { action_id: '0123456789abcdef', character: 'Testmage', generation: 'session-a' }
    action = { action_id: 'aaaaaaaaaaaaaaaa', character: 'Testmage', generation: 'session-a',
      command: 'lab-test-quick retreat 0123456789abcdef', expected_room_id: '1000', expires_at: 102.0 }
    authority = action.merge(status: 'dispatched', stop_requested: false)
    binding = LabControllerControls::Binding.new(controller: registry.controller('quick'),
      launch: launch, instance: @owner, available: ->(_) { true },
      authority: ->(_) { authority }, clock: -> { @now })
    binding.bind(@owner, @runtime)
    result = binding.request(action: action, match: registry.match(action[:command]))
    assert_equal 'control_queued', result[:code]
    assert_nil result[:details][:applied]
    assert_equal 'ordinary_stop', @runtime.poll
    assert @runtime.permitted?
    refute @runtime.work_permitted?
  ensure
    binding&.close
  end

  def test_real_lab_launch_lease_separates_return_intent_and_revocation
    action = { action_id: '0123456789abcdef', character: 'Testmage', generation: 'session-a',
      expires_at: 101.0, controller_deadline: 200.0 }
    authority = action.merge(status: 'completed', stop_requested: false)
    lease = LabControllerControls::Lease.new(action: action, deadline: 200.0,
      available: ->(_) { true }, authority: ->(_) { authority }, clock: -> { @now })
    assert lease.refresh
    assert @runtime.activate_supervised(valid: -> { lease.valid? })
    assert @runtime.await_supervisor!
    authority[:return_requested] = true
    assert lease.refresh
    assert lease.return_requested?
    @runtime.request('stop', valid: -> { lease.valid? })
    assert_equal 'ordinary_stop', @runtime.poll
    assert @runtime.permitted?
    authority[:stop_requested] = true
    refute lease.refresh
    refute @runtime.permitted?
  end

  def test_stop_queues_without_acting_then_owner_applies_return
    activate
    response = Thread.new { @runtime.request('stop', valid: -> { true }) }.value
    assert response[:accepted]
    refute @runtime.work_permitted?
    assert @runtime.permitted?
    assert_equal 'ordinary_stop', @runtime.poll
    assert @runtime.permitted?
    refute @runtime.work_permitted?
  end

  def test_expired_or_raising_control_never_requests_return
    activate
    @runtime.request('retreat', valid: -> { false })
    @runtime.request('stop', valid: -> { raise 'expired' })
    assert_nil @runtime.poll
    assert @runtime.work_permitted?
  end

  def test_controls_bounded_and_hold_not_supported
    activate
    refute @runtime.request('hold')[:accepted]
    refute @runtime.request('resume')[:accepted]
    refute @runtime.request('stop', valid: true)[:accepted]
    32.times { assert @runtime.request('stop')[:accepted] }
    refute @runtime.request('stop')[:accepted]
    assert @runtime.request('status')[:accepted]
  end

  def test_expiry_returns_without_renewing_authority
    activate
    @now = 150.0
    refute @runtime.work_permitted?
    assert_equal 'operation_work_deadline', @runtime.poll
    assert @runtime.permitted?
    @now = 200.0
    refute @runtime.permitted?
    assert_raises(LabPairedTrial::Invalid) { @runtime.poll }
  end

  def test_revocation_latches_and_does_not_turn_into_return
    activate
    @authorized = false
    refute @runtime.permitted?
    @authorized = true
    refute @runtime.permitted?
    assert_raises(LabPairedTrial::Invalid) { @runtime.poll }
  end

  def test_session_change_refuses_even_with_same_character_and_room
    activate
    @observed[:session] = 'replacement'
    refute @runtime.permitted?
    assert_equal :stopped, finish[:state]
  end

  def test_only_owner_can_mutate_lifecycle
    activate
    error = Thread.new do
      @runtime.poll
    rescue ThreadError => e
      e
    end.value
    assert_instance_of ThreadError, error
  end

  def test_success_requires_pair_receipts_and_fresh_local_handoff
    activate
    result = finish
    assert_equal :completed, result[:state]
    assert_equal true, result[:refuge][:returned]
    assert_equal true, result[:refuge][:equipment_restored]
    assert_equal true, result[:pair][:safe]
    assert_same result, @runtime.close
    refute @runtime.permitted?
  end

  def test_missing_peer_fails_work_but_preserves_true_local_recovery
    activate
    result = shared
    result[:receipts].pop
    value = finish(result: result)
    assert_equal :stopped, value[:state]
    assert_equal true, value[:refuge][:returned]
    assert_equal false, value[:pair][:safe]
  end

  def test_unclean_children_wrong_hands_and_stale_readiness_are_not_safe
    [:children, :hands, :ready].each do |case_name|
      setup
      activate
      @observed[:hands] = ['789', nil] if case_name == :hands
      @observed[:ready] = false if case_name == :ready
      value = finish(cleaned: case_name != :children)
      assert_equal :stopped, value[:state]
      refute value[:refuge][:returned]
      refute value[:refuge][:equipment_restored]
    end
  end

  def test_foreign_receipt_cannot_claim_local_success
    activate
    receipt = local_receipt.merge(identity: @peer)
    value = finish(local: receipt)
    assert_equal :stopped, value[:state]
    refute value[:refuge][:returned]
  end

  def test_failed_case_does_not_become_pass_after_return
    activate
    value = finish(result: shared.merge(success: false, reason: 'target_lost'))
    assert_equal :stopped, value[:state]
    assert value[:refuge][:returned]
    assert_equal 'target_lost', value[:work_result][:reason]
  end

  def test_close_without_receipt_never_fabricates_recovery
    activate
    value = @runtime.close
    assert_equal :stopped, value[:state]
    refute value[:refuge][:returned]
  end
end
