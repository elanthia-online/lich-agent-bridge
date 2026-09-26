require 'json'
require 'minitest/autorun'
require 'tempfile'

load File.expand_path('../lich/lab-controller-registry.rb', __dir__)

class LabControllerRegistryTest < Minitest::Test
  def test_eohunter_example_is_opt_in_bounded_and_exact
    path = File.expand_path('../examples/controllers/eohunter-trial-campaign.json', __dir__)
    controller = LabControllerRegistry.load(path).controller('eohunter-trial')
    assert_equal 'controller_refuge', controller.safe_handoff['kind']
    built = controller.action('start').build('profile' => 'Reviewed-Trial', 'trial' => 'a-b-c')
    assert_equal 'eohunter Reviewed-Trial trial a-b-c', built[:command]
    assert_equal 'Reviewed-Trial trial a-b-c', built[:script_args]
    assert_raises(LabControllerRegistry::ManifestError) do
      controller.action('start').build('profile' => 'Reviewed-Trial', 'trial' => 'a-b-d')
    end
  end

  def test_quick_refuge_requires_exact_room_return_budget_and_native_area_contract
    raw = JSON.parse(File.read(File.expand_path('fixtures/controller-controls.json', __dir__)))['controllers'].first
    raw.merge!('script' => 'bigshot', 'control_owner_scripts' => ['bigshot'],
      'safe_handoff' => { 'kind' => 'quick_refuge', 'room_id' => '1000', 'return_seconds' => 30 })
    raw['actions'].first['script_args_template'] = 'quick seek --area profile'
    assert LabControllerRegistry::Controller.new(raw, 'test')
    changes = [
      ->(c) { c['safe_handoff'].delete('return_seconds') },
      ->(c) { c['safe_handoff']['return_seconds'] = 9 },
      ->(c) { c['safe_handoff']['return_seconds'] = 121 },
      ->(c) { c['safe_handoff']['return_seconds'] = '30' },
      ->(c) { c['safe_handoff']['room_id'] = 0 },
      ->(c) { c['safe_handoff']['room_id'] = '4' },
      ->(c) { c['safe_handoff']['room_id'] = 1.5 },
      ->(c) { c['safe_handoff']['rooms'] = {} },
      ->(c) { c['lanes'] = ['combat'] },
      ->(c) { c['script'] = 'lab-test-quick' },
      ->(c) { c['actions'].first['script_args_template'] = 'quick clear' },
      ->(c) { c['actions'].first['script_args_template'] = 'quick seek --area profile --supervised-refuge-v1 1,2' }
    ]
    changes.each do |change|
      invalid = JSON.parse(JSON.generate(raw))
      change.call(invalid)
      assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(invalid, 'test') }
    end
  end

  def test_seek_requires_profile_area_and_both_movement_and_combat_lanes
    %w[seek SEEK {mode}].each do |mode|
      raw = JSON.parse(File.read(File.expand_path('fixtures/controller-controls.json', __dir__)))['controllers'].first
      raw.merge!('script' => 'bigshot', 'safe_handoff' => { 'kind' => 'quick_area' }, 'control_owner_scripts' => ['bigshot'])
      launch = raw['actions'].first
      launch.merge!('script_args_template' => "quick #{mode} --area profile", 'command_template' => "bigshot quick #{mode} --area profile")
      launch['parameters'] = [{ 'name' => 'mode', 'type' => 'enum', 'values' => %w[clear seek] }] if mode == '{mode}'
      assert LabControllerRegistry::Controller.new(raw, 'test')
      [->(c) { c['lanes'] = ['combat'] }, ->(c) { c['lanes'] = ['movement'] },
       ->(c) { c['safe_handoff'] = { 'kind' => 'room', 'room_id' => '1000' } }].each do |change|
        changed = JSON.parse(JSON.generate(raw))
        change.call(changed)
        assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(changed, 'test') }
      end
    end
  end

  def test_quick_area_requires_native_controlled_explicit_profile_launch_without_room_lists
    raw = JSON.parse(File.read(File.expand_path('fixtures/controller-controls.json', __dir__)))['controllers'].first
    raw['script'] = 'bigshot'
    raw['safe_handoff'] = { 'kind' => 'quick_area' }
    raw['control_owner_scripts'] = ['bigshot']
    raw['actions'].first['script_args_template'] = 'quick watch --area profile'
    assert_equal 'quick_area', LabControllerRegistry::Controller.new(raw, 'test').safe_handoff['kind']
    mutations = [
      ->(c) { c['script'] = 'lab-test-quick' },
      ->(c) { c['control_owner_scripts'] = [] },
      ->(c) { c['safe_handoff']['rooms'] = { 'test' => '1000' } },
      ->(c) { c['actions'].first['script_args_template'] = 'quick use reviewed' },
      ->(c) { c['actions'].first['script_args_template'] = 'quick watch --area off' },
      ->(c) { c['actions'].first['script_args_template'] = 'quick watch --area profile --area off' },
      ->(c) { c['actions'].first['script_args_template'] = 'quick watch --area=profile' },
      ->(c) { c['actions'].first['script_args_template'] = 'quick watch -- --area profile' },
      lambda { |c|
        c['actions'].first.merge!('command_template' => 'lab-test-quick start{extra}',
          'script_args_template' => 'quick watch --area profile {extra}',
          'parameters' => [{ 'name' => 'extra', 'type' => 'flag_suffix', 'true_value' => ' --area off' }])
      }
    ]
    mutations.each do |mutation|
      changed = JSON.parse(JSON.generate(raw))
      mutation.call(changed)
      assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(changed, 'test') }
    end
  end

  def test_test_registration_rejects_changed_command_policy_and_metadata
    raw = {
      'name' => 'test-probe', 'script' => 'lab-test-runner', 'summary' => 'Synthetic lifecycle test.',
      'characters' => ['Testmage'], 'result_global' => '$lab_test_result', 'signal_global' => '$lab_test_cancel',
      'lanes' => %w[movement combat], 'owner_scripts' => %w[lab-test-runner probe],
      'safe_handoff' => {'kind' => 'room', 'room_id' => '1000'}, 'capability_action' => 'start',
      'test_suite' => {'manifest' => 'suite.json', 'files' => %w[suite.json probe.lic lab-test-runner.lic lab-test-runner.rb].to_h { |name| [name, 'a' * 64] }},
      'actions' => [{'name' => 'start', 'kind' => 'launch', 'launch_mode' => 'start',
                     'command_template' => 'lab-test probe {revision} {case_id}',
                     'script_args_template' => 'probe {revision} {case_id}',
                     'policy' => {'category' => 'configuration', 'confirmation_required' => true},
                     'parameters' => [{'name' => 'revision', 'type' => 'enum', 'values' => ['a' * 64]},
                                      {'name' => 'case_id', 'type' => 'enum', 'values' => %w[normal all]}]}]
    }
    assert LabControllerRegistry::Controller.new(raw, 'test').test_suite
    mutations = [
      ->(data) { data['actions'][0]['command_template'] = 'something-else {revision} {case_id}' },
      ->(data) { data['actions'][0]['policy']['confirmation_required'] = false },
      ->(data) { data['actions'][0]['parameters'][0]['values'] = ['b' * 64] },
      ->(data) { data['characters'] << 'Othermage' },
      ->(data) { data['test_suite']['manifest'] = 'probe.lic' }
    ]
    mutations.each do |mutation|
      changed = JSON.parse(JSON.generate(raw))
      mutation.call(changed)
      assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(changed, 'test') }
    end
  end

  def setup
    @path = File.expand_path('fixtures/controllers.json', __dir__)
    @registry = LabControllerRegistry.load(@path)
  end

  def test_shipped_registry_is_empty_and_does_not_allow_test_controllers
    registry = LabControllerRegistry.load(File.expand_path('../lich/lab-controllers.json', __dir__))
    assert_empty registry.controllers
    assert_empty registry.safe_patterns
    assert_nil registry.match('lab-test-hunt start test-hunt')
  end

  def test_manifest_exposes_all_controllers_and_testknight_hunt_contract
    assert_equal %w[engage room hunt rift], @registry.controllers.map(&:name)
    hunt = @registry.controller('hunt')
    assert_equal ['Testknight'], hunt.characters
    assert_equal %w[movement combat], hunt.lanes
    assert_equal '$lab_hunt_result', hunt.result_global
    assert_equal 'profile_room', hunt.safe_handoff.fetch('kind')
    assert_equal '1000', hunt.safe_handoff.fetch('rooms').fetch('test-hunt')
  end

  def test_exact_anchored_grammar_matches_and_canonicalizes_arguments
    engagement = @registry.match('LAB-TEST-ENGAGE TEST-LIVING #185539534 LOOT')
    assert_equal 'engage', engagement.controller.name
    assert_equal 'start', engagement.action.name
    assert_equal 'test-living', engagement.arguments.fetch('profile')
    assert_equal '185539534', engagement.arguments.fetch('target_id')
    assert engagement.arguments.fetch('loot')
    assert_equal 'test-living #185539534 loot', engagement.script_args

    hunt = @registry.match('lab-test-hunt start test-hunt')
    assert_equal({ 'profile' => 'test-hunt' }, hunt.arguments)
    assert_nil @registry.match('lab-test-hunt start other-profile')
    assert_nil @registry.match('lab-test-hunt start test-hunt; north')
  end

  def test_capability_action_builds_exact_command_and_schema
    action = @registry.controller('hunt').action('start')
    built = action.build(profile: 'test-probe')
    assert_equal 'lab-test-hunt start test-probe', built.fetch(:command)
    assert_equal false, action.argument_schema.fetch('additionalProperties')
    assert_raises(LabControllerRegistry::ManifestError) do
      action.build(profile: 'test-hunt', extra: 'no')
    end
  end

  def test_invalid_manifest_fails_closed
    raw = JSON.parse(File.read(@path))
    raw.fetch('controllers').first['surprise'] = true
    file = Tempfile.new(['bad-controller-manifest', '.json'])
    file.write(JSON.generate(raw))
    file.close

    error = assert_raises(LabControllerRegistry::ManifestError) do
      LabControllerRegistry.load(file.path)
    end
    assert_match(/unsupported field/, error.message)
  ensure
    file&.unlink
  end
end
