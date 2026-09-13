# Offline full-access command checks: no game connection or real command sends.
require_relative 'lab_bridge_test'

class LabFullAccessTest < Minitest::Test
  def test_fresh_bridge_defaults_to_explicit_guarded_status
    statuses = LichAgentBridge.instance_variable_get(:@script_status)

    assert_equal 'guarded', statuses['lab-access']
  end

  def with_direct_action(enabled:)
    saved = {}
    %i[fput publish_snapshot report_action set_script_status].each do |name|
      saved[name] = LichAgentBridge.method(name) if LichAgentBridge.respond_to?(name, true)
    end
    sent = []
    reports = []
    statuses = {}
    old_enabled = LichAgentBridge.instance_variable_get(:@actions_enabled)
    old_full = LichAgentBridge.instance_variable_get(:@full_access)
    LichAgentBridge.instance_variable_set(:@actions_enabled, true)
    LichAgentBridge.instance_variable_set(:@full_access, enabled)
    LichAgentBridge.define_singleton_method(:fput) { |command| sent << command }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |**_args| true }
    LichAgentBridge.define_singleton_method(:set_script_status) { |name, value| statuses[name] = value }
    LichAgentBridge.define_singleton_method(:report_action) { |_action, outcome, detail| reports << [outcome, detail] }
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      generation: LichAgentBridge.session_generation,
      command: 'lab direct join Calvix',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }
    yield action, sent, reports, statuses
  ensure
    saved&.each { |name, method| LichAgentBridge.define_singleton_method(name, method) }
    LichAgentBridge.instance_variable_set(:@actions_enabled, old_enabled)
    LichAgentBridge.instance_variable_set(:@full_access, old_full)
  end

  def test_locally_enabled_full_access_delivers_one_game_command_and_receipt
    with_direct_action(enabled: true) do |action, sent, reports, statuses|
      LichAgentBridge.execute_action(action)

      assert_equal ['join Calvix'], sent
      assert_equal 'completed:1234567890abcdef', statuses['lab-direct']
      assert_equal 'completed', reports.last.first
      assert_match(/effect unverified/, reports.last.last)
    end
  end

  def test_full_access_cannot_be_enabled_by_the_brokered_command
    with_direct_action(enabled: false) do |action, sent, reports, statuses|
      LichAgentBridge.execute_action(action)

      assert_empty sent
      assert_empty statuses
      assert_equal 'failed', reports.last.first
      assert_match(/full access is off/, reports.last.last)
    end
  end

  def test_disabling_actions_revokes_local_full_access
    original_request = LichAgentBridge.method(:action_request)
    original_publish = LichAgentBridge.method(:publish_snapshot)
    LichAgentBridge.instance_variable_set(:@full_access, true)
    LichAgentBridge.instance_variable_set(:@script_status, { 'lab-access' => 'full' })
    LichAgentBridge.define_singleton_method(:action_request) do |_path, _body|
      { enabled: false, character: 'Testmage' }
    end
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |**_args| true }

    LichAgentBridge.set_actions(false)

    refute LichAgentBridge.instance_variable_get(:@full_access)
    assert_equal 'guarded', LichAgentBridge.instance_variable_get(:@script_status)['lab-access']
  ensure
    LichAgentBridge.define_singleton_method(:action_request, original_request)
    LichAgentBridge.define_singleton_method(:publish_snapshot, original_publish)
  end

  def test_native_gate_rejects_client_commands_and_chaining
    assert LichAgentBridge.safe_action_command?('lab direct join Calvix')
    refute LichAgentBridge.safe_action_command?("lab direct join Calvix\nquit")

    [', ask something', ';e puts 1', 'join Calvix;drop all', 'join Calvix|quit'].each do |inner|
      with_direct_action(enabled: true) do |action, sent, reports, _statuses|
        action[:command] = "lab direct #{inner}"
        LichAgentBridge.execute_action(action)
        assert_empty sent, inner
        assert_equal 'failed', reports.last.first, inner
      end
    end
  end
end
