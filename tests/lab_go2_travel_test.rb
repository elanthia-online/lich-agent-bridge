# Synthetic native-child seam; no game connection or wire sends.
require_relative 'lab_bridge_test'

class LabGo2TravelTest < Minitest::Test
  class Child
    attr_accessor :thread, :error
    attr_reader :kills
    def initialize = (@kills = 0)
    def name = 'go2'
    def join(timeout) = thread&.join(timeout)
    def completed_successfully? = !thread.alive? && error.nil? && kills.zero?
    def kill_sync(timeout:)
      @kills += 1
      thread.kill
      !!thread.join(timeout)
    end
  end

  def with_travel(wires: ['<c>north'], arrive: true, before_send: nil, stalled: false, competing: false, seconds: 2)
    owner = Struct.new(:child_scripts).new([])
    owner.define_singleton_method(:stopping?) { false }
    child = nil
    sent, reports, statuses, options_seen = [], [], {}, []
    room = '1000'
    saved = {}
    patch = lambda do |object, name, &implementation|
      saved[[object, name]] = object.method(name) if object.respond_to?(name, true)
      saved[[object, name]] ||= nil
      object.define_singleton_method(name, &implementation)
    end
    flags = %i[@running @actions_enabled @snapshot_generation_rejected].to_h { |name| [name, LichAgentBridge.instance_variable_get(name)] }
    old_prefix, old_dir = $cmd_prefix, $script_dir
    constants = %i[START_EXECUTION_GUARD_PROTOCOL SCRIPT_START_RESTRICTION_PROTOCOL].to_h { |name| [name, Script.const_defined?(name) ? Script.const_get(name) : nil] }
    constants.each_key { |name| Script.const_set(name, 1) unless Script.const_defined?(name) }
    Dir.mktmpdir do |directory|
      File.write(File.join(directory, 'go2.lic'), '# synthetic --preserve-scripts compatibility declaration')
      $script_dir, $cmd_prefix = directory, '<c>'
      LichAgentBridge.instance_variable_set(:@running, true)
      LichAgentBridge.instance_variable_set(:@actions_enabled, true)
      LichAgentBridge.instance_variable_set(:@snapshot_generation_rejected, false)
      patch.call(Map, :[]) { |id| id == 1001 ? Map::Room.new(id) : nil }
      patch.call(Script, :__find_script_file) { |_name| 'go2.lic' }
      patch.call(Script, :current) { owner }
      patch.call(Script, :hidden) { [] }
      patch.call(Script, :running) do
        items = child && child.thread&.alive? ? [child] : []
        items << Struct.new(:name).new('go2') if competing
        items
      end
      patch.call(LichAgentBridge, :inventory_tracker_activity) { false }
      patch.call(LichAgentBridge, :room_fingerprint) { room }
      patch.call(LichAgentBridge, :publish_snapshot) { |**_args| nil }
      patch.call(LichAgentBridge, :set_script_status) { |name, value| statuses[name] = value }
      patch.call(LichAgentBridge, :report_action) { |_action, status, detail| reports << [status, detail] }
      action = {action_id: '1234567890abcdef', character: 'Testmage', generation: LichAgentBridge.session_generation,
                expected_room_id: '1000', command: 'go2 supervised 1001', expires_at: Time.now.to_f + seconds}
      patch.call(LichAgentBridge, :controlled_action_authority) { |_id| action.merge(status: 'dispatched', stop_requested: false) }
      patch.call(Script, :start_child) do |name, arguments, options|
        options_seen << [name, arguments, options]
        child = Child.new
        owner.child_scripts << child
        child.thread = Thread.new do
          begin
            sleep 0.2 if stalled
            wires.each do |wire|
              before_send&.call
              raise 'guard rejected' unless options.fetch(:execution_guard).call(wire)
              sent << wire
            end
            room = '1001' if arrive
          rescue StandardError => error
            child.error = error
          end
        end
        child
      end
      LichAgentBridge.execute_go2(action, '1001')
      yield sent, reports, statuses, child, options_seen
    end
  ensure
    child&.kill_sync(timeout: 0.2) if child&.thread&.alive?
    saved&.each { |(object, name), method| method ? object.define_singleton_method(name, method) : object.singleton_class.send(:remove_method, name) }
    flags&.each { |name, value| LichAgentBridge.instance_variable_set(name, value) }
    constants&.each { |name, value| Script.send(:remove_const, name) if value.nil? }
    $cmd_prefix, $script_dir = old_prefix, old_dir
  end

  def test_exact_guarded_child_arrives_without_changing_global_go2_settings
    with_travel do |sent, reports, statuses, child, calls|
      assert_equal ['<c>north'], sent
      assert_equal 'completed', reports.last.first, reports.inspect
      assert_equal 'completed:1234567890abcdef', statuses['go2']
      assert_equal 0, child.kills
      assert_equal 'go2', calls.first[0]
      assert_includes calls.first[1], '--preserve-scripts'
      assert_equal false, calls.first[2][:allow_script_starts]
      assert_kind_of Proc, calls.first[2][:execution_guard]
    end
  end

  def test_wrong_destination_is_failed_even_when_child_exits_normally
    with_travel(arrive: false) { |_sent, reports| assert_equal 'failed', reports.last.first }
  end

  def test_competing_go2_is_never_adopted_or_killed
    with_travel(competing: true) do |sent, reports, _statuses, child, calls|
      assert_empty sent
      assert_empty calls
      assert_nil child
      assert_equal 'failed', reports.last.first
    end
  end

  def test_map_edge_commands_are_delegated_to_the_exact_go2_child
    wires = ['<c>go gate', '<c>prepare 407', '<c>cast gate', '<c>push bronze gate']
    with_travel(wires: wires) do |sent, reports|
      assert_equal wires, sent
      assert_equal 'completed', reports.last.first, reports.inspect
    end
  end

  def test_malformed_wire_commands_fail_before_any_send
    ['', 'north', '<c>', "<c>north\nsouth", "<c>north\rsouth", "<c>#{'x' * 1025}"].each do |wire|
      with_travel(wires: [wire]) do |sent, reports|
        assert_empty sent, wire
        assert_equal 'failed', reports.last.first, reports.inspect
      end
    end
  end

  def test_actions_off_before_send_fails_closed
    with_travel(before_send: -> { LichAgentBridge.instance_variable_set(:@actions_enabled, false) }) do |sent, reports|
      assert_empty sent
      assert_equal 'failed', reports.last.first
    end
  end

  def test_send_budget_is_enforced_locally
    with_travel(wires: Array.new(257, '<c>north')) do |sent, reports|
      assert_equal 256, sent.length
      assert_equal 'failed', reports.last.first
    end
  end

  def test_supervised_selector_is_distinct_from_legacy_travel
    assert LichAgentBridge.safe_action_command?('go2 supervised 1001')
    assert LichAgentBridge.safe_action_command?('go2 1001')
    refute LichAgentBridge.safe_action_command?('go2 supervised 1001;look')
    refute LichAgentBridge.safe_action_command?('go2 supervised town')
  end

  def test_deadline_cancels_only_exact_child
    with_travel(stalled: true, seconds: 0.08) do |sent, reports, _statuses, child|
      assert_empty sent
      assert_equal 'failed', reports.last.first
      assert_equal 1, child.kills
      refute child.thread.alive?
    end
  end
end
