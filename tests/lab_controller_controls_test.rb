require 'json'
require 'minitest/autorun'
require_relative '../lich/lab-controller-registry'

# Shared synthetic registration, never an installed controller or game fixture.
class LabControllerControlsTest < Minitest::Test
  FIXTURE = File.expand_path('fixtures/controller-controls.json', __dir__)
  RUN_ID = '0123456789abcdef'

  def setup
    @registry = LabControllerRegistry.load(FIXTURE)
  end

  def test_exact_run_controls_use_the_same_schema_as_python
    %w[status hold resume retreat].each do |verb|
      action = @registry.controller('quick').action(verb)
      built = action.build(run_id: RUN_ID)
      assert_equal "lab-test-quick #{verb} #{RUN_ID}", built[:command]
      assert_equal '', built[:script_args]
      assert_equal({ 'run_id' => RUN_ID }, @registry.match(built[:command]).arguments)
      assert_equal 'control', @registry.match(built[:command]).action.kind
    end
  end

  def test_invalid_tokens_unknown_controls_and_extra_arguments_fail_closed
    action = @registry.controller('quick').action('hold')
    [nil, true, 1234567890123456, '', RUN_ID.upcase, "#{RUN_ID}0", " #{RUN_ID}", "#{RUN_ID};north"].each do |value|
      assert_raises(LabControllerRegistry::ManifestError) { action.build(run_id: value) }
      assert_nil @registry.match("lab-test-quick hold #{value}") unless value.is_a?(Integer)
    end
    assert_raises(LabControllerRegistry::ManifestError) { action.build(run_id: RUN_ID, command: 'attack') }
    assert_nil @registry.match("lab-test-quick stop #{RUN_ID}")
    refute @registry.safe_patterns.any? { |pattern| pattern.match?("lab-test-quick hold #{RUN_ID.upcase}") }
  end

  def test_control_schema_rejects_expanded_authority
    mutations = [
      ->(action) { action['name'] = 'stop' },
      ->(action) { action['script_args_template'] = 'hold' },
      ->(action) { action['launch_mode'] = 'run' },
      ->(action) { action['parameters'] = [] },
      ->(action) { action['parameters'][0]['type'] = 'numeric' },
      ->(action) { action['parameters'][0]['values'] = [RUN_ID] },
      ->(action) { action['policy']['category'] = 'inspection' },
      ->(action) { action['policy']['confirmation_required'] = false }
    ]
    mutations.each do |mutate|
      raw = JSON.parse(File.read(FIXTURE))['controllers'].first
      mutate.call(raw['actions'][2])
      assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(raw, 'test') }
    end
  end

  def test_control_is_not_an_independent_launch_capability
    raw = JSON.parse(File.read(FIXTURE))['controllers'].first
    raw['capability_action'] = 'hold'
    assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(raw, 'test') }
  end

  def test_shipped_registry_still_advertises_no_controls
    registry = LabControllerRegistry.load(File.expand_path('../lich/lab-controllers.json', __dir__))
    assert_empty registry.controllers
    assert_nil registry.match("lab-test-quick hold #{RUN_ID}")
  end

  def test_control_owners_are_explicit_not_the_entire_exclusion_list
    [nil, [], ['bigshot'], %w[lab-test-quick unregistered-script]].each do |owners|
      raw = JSON.parse(File.read(FIXTURE))['controllers'].first
      owners.nil? ? raw.delete('control_owner_scripts') : raw['control_owner_scripts'] = owners
      assert_raises(LabControllerRegistry::ManifestError) { LabControllerRegistry::Controller.new(raw, 'test') }
    end
  end
end
