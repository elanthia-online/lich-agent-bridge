# Offline full-access command checks: no game connection or real command sends.
require_relative 'lab_bridge_test'

class LabFullAccessTest < Minitest::Test
  def with_saved_access
    records = {}
    backend = Object.new
    backend.define_singleton_method(:current_script_settings) { |scope, script_name:| (records[[script_name, scope]] || {}).dup }
    backend.define_singleton_method(:get_scoped_setting) { |scope, key, script_name:| current_script_settings(scope, script_name: script_name)[key] }
    backend.define_singleton_method(:save_to_database) { |data, scope, script_name:| records[[script_name, scope]] = data.dup }
    old_full = LichAgentBridge.instance_variable_get(:@full_access)
    old_status = LichAgentBridge.instance_variable_get(:@script_status).dup
    saved = %i[full_access_settings publish_snapshot].to_h { |name| [name, LichAgentBridge.method(name)] }
    LichAgentBridge.define_singleton_method(:full_access_settings) { backend }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |**_args| true }
    yield backend, records
  ensure
    saved&.each { |name, method| LichAgentBridge.define_singleton_method(name, method) }
    LichAgentBridge.instance_variable_set(:@full_access, old_full)
    LichAgentBridge.instance_variable_set(:@script_status, old_status)
  end

  def test_explicit_choice_survives_runtime_reset_and_preserves_other_settings
    with_saved_access do |_backend, records|
      records[['lab', 'GSIV:Testmage']] = { 'unrelated' => 42 }
      assert LichAgentBridge.set_full_access(true)
      LichAgentBridge.instance_variable_set(:@full_access, false)
      assert LichAgentBridge.restore_full_access
      assert_equal 42, records[['lab', 'GSIV:Testmage']]['unrelated']
      refute LichAgentBridge.set_full_access(false)
      refute LichAgentBridge.restore_full_access
    end
  end

  def test_preference_is_character_and_game_scoped_and_requires_literal_true
    with_saved_access do |_backend, records|
      refute LichAgentBridge.restore_full_access
      records[['lab', 'GSIV:Testmage']] = { 'full_access' => true }
      original_name, original_game = XMLData.method(:name), XMLData.method(:game)
      begin
        XMLData.define_singleton_method(:name) { 'Othermage' }
        refute LichAgentBridge.restore_full_access
        XMLData.define_singleton_method(:name, original_name)
        XMLData.define_singleton_method(:game) { 'GSPlat' }
        refute LichAgentBridge.restore_full_access
      ensure
        XMLData.define_singleton_method(:name, original_name)
        XMLData.define_singleton_method(:game, original_game)
      end
      [nil, false, 'true', 1].each do |value|
        records[['lab', 'GSIV:Testmage']] = { 'full_access' => value }
        refute LichAgentBridge.restore_full_access
      end
    end
  end

  def test_settings_failure_never_keeps_runtime_access_enabled
    with_saved_access do |backend, _records|
      assert LichAgentBridge.set_full_access(true)
      backend.define_singleton_method(:save_to_database) { |*args, **kwargs| raise IOError, 'test failure' }
      refute LichAgentBridge.set_full_access(false)
      refute LichAgentBridge.instance_variable_get(:@full_access)
      backend.define_singleton_method(:get_scoped_setting) { |*args, **kwargs| raise IOError, 'test failure' }
      refute LichAgentBridge.restore_full_access
    end
  end

  def test_fresh_bridge_defaults_to_explicit_guarded_status
    with_saved_access do
      refute LichAgentBridge.restore_full_access
      assert_equal 'guarded', LichAgentBridge.instance_variable_get(:@script_status)['lab-access']
    end
  end

  def with_direct_action(enabled:)
    saved = {}
    %i[action_request display fput dispatch_lich_client_command publish_snapshot report_action set_script_status].each do |name|
      saved[name] = LichAgentBridge.method(name) if LichAgentBridge.respond_to?(name, true)
    end
    sent = []
    client_commands = []
    reports = []
    statuses = {}
    old_enabled = LichAgentBridge.instance_variable_get(:@actions_enabled)
    old_full = LichAgentBridge.instance_variable_get(:@full_access)
    LichAgentBridge.instance_variable_set(:@actions_enabled, true)
    LichAgentBridge.instance_variable_set(:@full_access, enabled)
    LichAgentBridge.define_singleton_method(:action_request) do |_path, payload|
      { enabled: payload.fetch(:enabled), character: 'Testmage' }
    end
    LichAgentBridge.define_singleton_method(:display) { |_message| nil }
    LichAgentBridge.define_singleton_method(:fput) { |command| sent << command }
    LichAgentBridge.define_singleton_method(:dispatch_lich_client_command) { |command| client_commands << command }
    LichAgentBridge.define_singleton_method(:publish_snapshot) { |**_args| true }
    LichAgentBridge.define_singleton_method(:set_script_status) { |name, value| statuses[name] = value }
    LichAgentBridge.define_singleton_method(:report_action) { |_action, outcome, detail| reports << [outcome, detail] }
    action = {
      action_id: '1234567890abcdef',
      character: 'Testmage',
      generation: LichAgentBridge.session_generation,
      command: 'lab direct join Testleader',
      expected_room_id: '1000',
      expires_at: Time.now.to_f + 10
    }
    yield action, sent, reports, statuses, client_commands
  ensure
    saved&.each { |name, method| LichAgentBridge.define_singleton_method(name, method) }
    LichAgentBridge.instance_variable_set(:@actions_enabled, old_enabled)
    LichAgentBridge.instance_variable_set(:@full_access, old_full)
  end

  def test_locally_enabled_full_access_delivers_one_game_command_and_receipt
    with_direct_action(enabled: true) do |action, sent, reports, statuses, _client_commands|
      LichAgentBridge.execute_action(action)

      assert_equal ['join Testleader'], sent
      assert_equal 'completed:1234567890abcdef', statuses['lab-direct']
      assert_equal 'completed', reports.last.first
      assert_match(/effect unverified/, reports.last.last)
    end
  end

  def test_locally_enabled_full_access_dispatches_one_lich_script_command
    with_direct_action(enabled: true) do |action, sent, reports, statuses, client_commands|
      action[:command] = 'lab direct ;eohunter Leveling-Trio dry'
      LichAgentBridge.execute_action(action)

      assert_empty sent
      assert_equal [';eohunter Leveling-Trio dry'], client_commands
      assert_equal 'completed:1234567890abcdef', statuses['lab-direct']
      assert_equal 'completed', reports.last.first
      assert_match(/effect unverified/, reports.last.last)
    end
  end

  def test_full_access_cannot_be_enabled_by_the_brokered_command
    with_direct_action(enabled: false) do |action, sent, reports, statuses, _client_commands|
      LichAgentBridge.execute_action(action)

      assert_empty sent
      assert_empty statuses
      assert_equal 'failed', reports.last.first
      assert_match(/full access is off/, reports.last.last)
    end
  end

  def test_actions_off_revokes_execution_without_erasing_saved_full_access
    with_direct_action(enabled: true) do |action, sent, reports, statuses, client_commands|
      LichAgentBridge.set_actions(false)

      assert LichAgentBridge.instance_variable_get(:@full_access)
      refute LichAgentBridge.instance_variable_get(:@actions_enabled)

      LichAgentBridge.execute_action(action)

      assert_empty sent
      assert_empty client_commands
      assert_empty statuses
      assert_equal ['failed', 'local kill switch is off'], reports.last
    end
  end

  def test_native_gate_rejects_client_commands_and_chaining
    assert LichAgentBridge.safe_action_command?('lab direct join Testleader')
    assert LichAgentBridge.safe_action_command?('lab direct ;eohunter Leveling-Trio dry')
    refute LichAgentBridge.safe_action_command?("lab direct join Testleader\nquit")

    [', ask something', ';e puts 1', ';exec puts 1', ';eohunter Smoke;quit', 'join Testleader;drop all', 'join Testleader|quit'].each do |inner|
      with_direct_action(enabled: true) do |action, sent, reports, _statuses, client_commands|
        action[:command] = "lab direct #{inner}"
        LichAgentBridge.execute_action(action)
        assert_empty sent, inner
        assert_empty client_commands, inner
        assert_equal 'failed', reports.last.first, inner
      end
    end
  end
end
