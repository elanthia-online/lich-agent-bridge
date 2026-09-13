# Offline native recovery checks: no game connection or commands.
require_relative 'lab_bridge_test'

class LabBridgeTest
  def test_controlled_recovery_ledger_survives_bridge_store_rebinding
    original_global_runs = $lab_controlled_runs
    original_global_mutex = $lab_controlled_run_mutex
    retained = { 'quick' => { unsafe_handoff: true, action: { action_id: '0123456789abcdef' } } }
    retained_mutex = Mutex.new
    $lab_controlled_runs = retained
    $lab_controlled_run_mutex = retained_mutex

    LichAgentBridge.bind_controlled_run_store
    assert_same retained, LichAgentBridge.instance_variable_get(:@controlled_runs)
    assert_same retained_mutex, LichAgentBridge.instance_variable_get(:@controlled_run_mutex)

    LichAgentBridge.instance_variable_set(:@controlled_runs, {})
    LichAgentBridge.instance_variable_set(:@controlled_run_mutex, Mutex.new)
    LichAgentBridge.bind_controlled_run_store
    assert_same retained, LichAgentBridge.instance_variable_get(:@controlled_runs)
    assert_same retained_mutex, LichAgentBridge.instance_variable_get(:@controlled_run_mutex)
    assert_equal '0123456789abcdef', LichAgentBridge.instance_variable_get(:@controlled_runs).dig('quick', :action, :action_id)
  ensure
    $lab_controlled_runs = original_global_runs
    $lab_controlled_run_mutex = original_global_mutex
    LichAgentBridge.bind_controlled_run_store if LichAgentBridge.respond_to?(:bind_controlled_run_store)
  end

  def with_failed_old_quick
    with_controlled_quick do |fixture|
      LichAgentBridge.execute_action(fixture[:action])
      run = LichAgentBridge.instance_variable_get(:@controlled_runs)['quick']
      fixture[:room] = '1001'
      fixture[:runtime].current_status = { state: :stopped, reason: 'return_blocked' }
      fixture[:child].alive = false
      assert run[:monitor].join(1)
      assert run[:unsafe_handoff]
      LichAgentBridge.instance_variable_set(:@session_generation, 'replacement-generation')
      fixture[:room] = '1000'
      original_async = LichAgentBridge.method(:run_command_async)
      LichAgentBridge.define_singleton_method(:run_command_async) { |&work| work.call }
      yield fixture, run
    ensure
      LichAgentBridge.define_singleton_method(:run_command_async, original_async) if original_async
    end
  end

  def test_operator_confirmation_reconciles_exact_old_run_without_changing_its_result
    with_failed_old_quick do |fixture, run|
      old_result = Marshal.load(Marshal.dump(run[:result]))
      assert LichAgentBridge.unresolved_controlled_handoff?
      LichAgentBridge.define_singleton_method(:run_command_async) { raise 'recovery must not use the command queue' }
      LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
      refute run[:unsafe_handoff], 'confirmed safe recovery must release this exact old-generation lock'
      assert_equal old_result, run[:result], 'recovery must not rewrite the failed test result'
      assert_equal "recovered:#{fixture[:action][:action_id]}",
        LichAgentBridge.instance_variable_get(:@script_status).fetch(run[:controller].script)
      assert_equal 1, fixture[:starts].size
      assert_empty fixture[:child].kills
      receipt = fixture[:http].find { |_, path, data| path == '/v1/event' && data[:kind] == 'controller_recovery' }
      refute_nil receipt
      assert_equal fixture[:action][:generation], receipt[2][:data][:previous_generation]
      assert_equal fixture[:action][:action_id], receipt[2][:data][:action_id]
    end
  end

  def test_brokered_recovery_uses_the_same_exact_native_verifier
    with_failed_old_quick do |fixture, run|
      old_result = Marshal.load(Marshal.dump(run[:result]))
      recovery = {
        action_id: 'aaaaaaaaaaaaaaaa',
        character: 'Testmage',
        generation: 'replacement-generation',
        command: "lab recover #{fixture[:action][:action_id]} confirm",
        expected_room_id: '1000',
        expires_at: Time.now.to_f + 1
      }

      LichAgentBridge.execute_action(recovery)

      refute run[:unsafe_handoff]
      assert_equal old_result, run[:result]
      receipt = fixture[:http].find { |_, path, data| path == '/v1/event' && data[:kind] == 'controller_recovery' }
      refute_nil receipt
      result = fixture[:results].find { |path, data| path == '/v1/actions/result' && data[:action_id] == recovery[:action_id] }
      refute_nil result
      assert_equal 'completed', result[1][:outcome]
      assert_match(/recovery acknowledged/, result[1][:detail])
    end
  end

  def test_brokered_recovery_reports_failure_when_receipt_cannot_publish
    with_failed_old_quick do |fixture, run|
      original = LichAgentBridge.method(:publish_event)
      LichAgentBridge.define_singleton_method(:publish_event) { |*_| false }
      recovery = {
        action_id: 'aaaaaaaaaaaaaaaa',
        character: 'Testmage',
        generation: 'replacement-generation',
        command: "lab recover #{fixture[:action][:action_id]} confirm",
        expected_room_id: '1000',
        expires_at: Time.now.to_f + 1
      }

      LichAgentBridge.execute_action(recovery)

      assert run[:unsafe_handoff]
      result = fixture[:results].find { |path, data| path == '/v1/actions/result' && data[:action_id] == recovery[:action_id] }
      refute_nil result
      assert_equal 'failed', result[1][:outcome]
      assert_match(/receipt was not acknowledged/, result[1][:detail])
    ensure
      LichAgentBridge.define_singleton_method(:publish_event, original) if original
    end
  end

  def test_recovery_listing_and_incomplete_confirmation_never_clear_the_lock
    with_failed_old_quick do |fixture, run|
      ['recover', "recover #{fixture[:action][:action_id]}", 'recover all confirm',
       'recover ffffffffffffffff confirm', "recover #{fixture[:action][:action_id]} confirm extra"].each do |command|
        LichAgentBridge.handle_command(command)
        assert run[:unsafe_handoff]
      end
      refute fixture[:http].any? { |_, path, data| path == '/v1/event' && data[:kind] == 'controller_recovery' }
    end
  end

  def test_recovery_refuses_unverified_native_handoff
    mutations = {
      outside_refuge: ->(f, _r) { f[:room] = '1001' },
      wrong_character: ->(f, _r) { f[:state_overrides][:character] = 'Othermage' },
      stale_generation: ->(f, _r) { f[:state_overrides][:generation] = 'older-generation' },
      wrong_hands: ->(f, _r) { f[:state_overrides][:hands] = { right: { id: '999' }, left: nil } },
      unknown_hands: ->(f, _r) { f[:state_overrides][:hands] = nil },
      dead: ->(f, _r) { f[:state_overrides][:dead] = true },
      stunned: ->(f, _r) { f[:state_overrides][:stunned] = true },
      no_health: ->(f, _r) { f[:state_overrides][:vitals] = { health: { current: 0 } } },
      not_standing: ->(f, _r) { f[:standing] = false },
      unjoined_child: ->(f, _r) { f[:child].cleanup_blocked = true },
      live_child: ->(f, _r) { f[:child].alive = true },
      unjoined_monitor: ->(_f, r) { r[:monitor] = Object.new.tap { |m| m.define_singleton_method(:join) { |_| nil } } },
      foreign_owner: ->(_f, _r) { Script.define_singleton_method(:running) { [Struct.new(:name).new('go2')] } }
    }
    mutations.each do |label, mutate|
      with_failed_old_quick do |fixture, run|
        mutate.call(fixture, run)
        LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
        assert run[:unsafe_handoff], label
        refute fixture[:http].any? { |_, path, data| path == '/v1/event' && data[:kind] == 'controller_recovery' }, label
      end
    end
  end

  def test_recovery_keeps_lock_on_receipt_failure_and_allows_exact_retry
    with_failed_old_quick do |fixture, run|
      original = LichAgentBridge.method(:publish_event)
      LichAgentBridge.define_singleton_method(:publish_event) { |*_| false }
      LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
      assert run[:unsafe_handoff]
      LichAgentBridge.define_singleton_method(:publish_event, original)
      LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
      refute run[:unsafe_handoff]
      LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
      assert_equal 2, fixture[:http].count { |_, path, data| path == '/v1/event' && data[:kind] == 'controller_recovery' }
    ensure
      LichAgentBridge.define_singleton_method(:publish_event, original) if original
    end
  end

  def test_recovery_does_not_clear_if_native_state_changes_during_publication
    with_failed_old_quick do |fixture, run|
      original = LichAgentBridge.method(:publish_event)
      LichAgentBridge.define_singleton_method(:publish_event) do |*args|
        result = original.call(*args)
        fixture[:room] = '1001'
        result
      end
      LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
      assert run[:unsafe_handoff]
    ensure
      LichAgentBridge.define_singleton_method(:publish_event, original) if original
    end
  end

  def test_recovery_does_not_enable_actions_or_automatic_approval
    with_failed_old_quick do |fixture, run|
      old_enabled = LichAgentBridge.instance_variable_get(:@actions_enabled)
      old_auto = LichAgentBridge.instance_variable_get(:@auto_approve)
      LichAgentBridge.instance_variable_set(:@actions_enabled, false)
      LichAgentBridge.instance_variable_set(:@auto_approve, false)
      LichAgentBridge.handle_command("recover #{fixture[:action][:action_id]} confirm")
      refute run[:unsafe_handoff]
      refute LichAgentBridge.instance_variable_get(:@actions_enabled)
      refute LichAgentBridge.instance_variable_get(:@auto_approve)
    ensure
      LichAgentBridge.instance_variable_set(:@actions_enabled, old_enabled)
      LichAgentBridge.instance_variable_set(:@auto_approve, old_auto)
    end
  end
end
