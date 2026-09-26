require 'minitest/autorun'
require 'tmpdir'
require_relative '../lich/lab-controller-registry'

ENV['LAB_INVENTORY_LIBRARY_ONLY'] = '1'
if ENV['LICH_EXECUTION_GUARD_ROOT']
  require File.join(ENV.fetch('LICH_EXECUTION_GUARD_ROOT'), 'lib/common/script_execution_guard')
end
DATA_DIR = Dir.mktmpdir('lab-inventory-test') unless defined?(DATA_DIR)

FakeInventoryObject = Struct.new(:id, :noun, :name, :full_name)

module XMLData
  class << self
    attr_accessor :character_name
  end

  def self.name = character_name || 'Testwarrior'
  def self.game = 'GSIV'
end

module GameObj
  class << self
    attr_accessor :inventory, :container_map, :right, :left
  end

  def self.inv = inventory
  def self.containers = container_map
  def self.right_hand = right
  def self.left_hand = left
end

module Lich
  def self.log(_message) = nil
end

def respond(_message) = nil

load File.expand_path('../lich/lab-inventory.lic', __dir__)

class LabInventoryTest < Minitest::Test
  def with_refresh_fixture(guarded: true, registry: LabControllerRegistry::Registry.new([], 'synthetic'))
    old_constants = %i[Script LichAgentBridge].to_h do |name|
      [name, (Object.const_get(name) if Object.const_defined?(name))]
    end
    old_constants.each_key { |name| Object.send(:remove_const, name) if Object.const_defined?(name) }
    owners = { movement: nil, combat: nil, inventory: 'lab-inventory' }
    sent = []
    owner = Object.new
    policy = nil
    guard = nil
    owner.define_singleton_method(:with_execution_guard) do |callback, &work|
      policy = callback
      guard = Lich::Common::ScriptExecutionGuard.new(callback) if ENV['LICH_EXECUTION_GUARD_ROOT']
      guard ? guard.checkpoint! : (raise 'guard denied' unless policy.call(nil) == true)
      work.call
    ensure
      guard&.close!
      guard = nil
      policy = nil
    end
    owner.define_singleton_method(:emit) do |command|
      if guard
        guard.checkpoint!(command: command)
      elsif guarded
        raise 'guard denied' unless policy && policy.call(command) == true
      end
      sent << command
    end
    owner.singleton_class.send(:remove_method, :with_execution_guard) unless guarded
    script = Module.new
    script.define_singleton_method(:current) { owner }
    Object.const_set(:Script, script)
    bridge = Module.new
    bridge.define_singleton_method(:live_scripts) { ['lab-inventory'] }
    bridge.define_singleton_method(:script_owners) { |_scripts| owners }
    bridge.const_set(:CONTROLLER_REGISTRY, registry) unless registry == :missing
    Object.const_set(:LichAgentBridge, bridge)
    yield owners, owner, sent
  ensure
    old_constants.each do |name, old|
      Object.send(:remove_const, name) if Object.const_defined?(name)
      Object.const_set(name, old) if old
    end
  end

  def test_refresh_claims_lane_then_returns_to_passive_even_after_failure
    with_refresh_fixture do |_owners, _owner, _sent|
      refute LabInventory.inventory_lane_active?
      assert_raises(RuntimeError) do
        LabInventory.with_inventory_action do
          assert LabInventory.inventory_lane_active?
          raise 'synthetic failure'
        end
      end
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_empty_registry_preserves_legacy_refresh_without_native_guards
    with_refresh_fixture(guarded: false) do |owners, owner, sent|
      checked = false
      LichAgentBridge.define_singleton_method(:script_owners) do |_scripts|
        raise 'lane was not claimed before ownership check' unless LabInventory.inventory_lane_active?
        checked = true
        owners
      end
      LabInventory.with_inventory_action do
        assert checked
        assert LabInventory.inventory_lane_active?
        owner.emit('inventory enhancive list')
      end
      assert_equal ['inventory enhancive list'], sent
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_legacy_refresh_checks_ownership_before_sending_and_releases_the_lane
    with_refresh_fixture(guarded: false) do |owners, owner, sent|
      [{ movement: 'go2' }, { combat: 'bigshot' }, { inventory: 'eloot' }, {}].each do |conflict|
        original = owners.dup
        conflict.empty? ? owners.clear : owners.merge!(conflict)
        assert_raises(StandardError) { LabInventory.with_inventory_action { owner.emit('must not send') } }
        assert_empty sent
        refute LabInventory.inventory_lane_active?
        owners.replace(original)
      end
      assert_raises(RuntimeError) do
        LabInventory.with_inventory_action do
          assert LabInventory.inventory_lane_active?
          raise 'synthetic refresh failure'
        end
      end
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_legacy_refresh_requires_an_explicitly_empty_validated_registry
    nonempty = LabControllerRegistry::Registry.new([Struct.new(:name, :script).new('test', 'test')], 'synthetic')
    malformed = LabControllerRegistry::Registry.new([], 'synthetic')
    malformed.define_singleton_method(:controllers) { nil }
    [nonempty, :missing, nil, {}, Struct.new(:controllers).new([]), malformed].each do |registry|
      with_refresh_fixture(guarded: false, registry: registry) do |_owners, owner, sent|
        assert_raises(StandardError) { LabInventory.with_inventory_action { owner.emit('must not send') } }
        assert_empty sent
        refute LabInventory.inventory_lane_active?
      end
    end
  end

  def test_legacy_refresh_does_not_assume_missing_bridge_means_no_controllers
    with_refresh_fixture(guarded: false) do |_owners, owner, sent|
      Object.send(:remove_const, :LichAgentBridge)
      assert_raises(StandardError) { LabInventory.with_inventory_action { owner.emit('must not send') } }
      assert_empty sent
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_legacy_refresh_keeps_the_exclusive_lane_claim
    with_refresh_fixture(guarded: false) do |_owners, _owner, _sent|
      LabInventory.with_inventory_action do
        assert_raises(RuntimeError) { LabInventory.with_inventory_action { flunk 'concurrent refresh admitted' } }
        assert LabInventory.inventory_lane_active?
      end
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_refresh_refuses_missing_current_script_even_with_empty_registry
    with_refresh_fixture(guarded: false) do |_owners, owner, sent|
      Script.define_singleton_method(:current) { nil }
      assert_raises(StandardError) { LabInventory.with_inventory_action { owner.emit('must not send') } }
      assert_empty sent
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_native_guards_remain_active_regardless_of_registry_availability
    nonempty = LabControllerRegistry::Registry.new([Struct.new(:name, :script).new('test', 'test')], 'synthetic')
    [nonempty, :missing].each do |registry|
      with_refresh_fixture(registry: registry) do |owners, owner, sent|
        assert_raises(StandardError) do
          LabInventory.with_inventory_action do
            owner.emit('first')
            owners[:combat] = 'bigshot'
            owner.emit('must not send')
          end
        end
        assert_equal ['first'], sent
        refute LabInventory.inventory_lane_active?
      end
    end
  end

  def test_refresh_refuses_competing_or_unknown_ownership_without_sends
    with_refresh_fixture do |owners, owner, sent|
      [{ movement: 'go2' }, { combat: 'bigshot' }, { inventory: 'eloot' }, { inventory: 'eherbs' }].each do |conflict|
        original = owners.dup
        owners.merge!(conflict)
        assert_raises(StandardError) { LabInventory.with_inventory_action { owner.emit('inventory enhancive list') } }
        refute LabInventory.inventory_lane_active?
        owners.replace(original)
      end
      owners.clear
      assert_raises(StandardError) { LabInventory.with_inventory_action { owner.emit('inventory enhancive list') } }
      assert_empty sent
    end
  end

  def test_refresh_rechecks_competitors_before_each_send
    with_refresh_fixture do |owners, owner, sent|
      assert_raises(StandardError) do
        LabInventory.with_inventory_action do
          owner.emit('first')
          owners[:combat] = 'bigshot'
          owner.emit('must not send')
        end
      end
      assert_equal ['first'], sent
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_rejected_concurrent_refresh_does_not_release_the_first_claim
    with_refresh_fixture do |_owners, _owner, _sent|
      LabInventory.with_inventory_action do
        error = Thread.new do
          LabInventory.with_inventory_action { flunk 'second refresh entered' }
        rescue RuntimeError => exception
          exception
        end.value
        assert_match(/already active/, error.message)
        assert LabInventory.inventory_lane_active?
      end
      refute LabInventory.inventory_lane_active?
    end
  end

  def test_charge_refresh_entry_point_uses_the_guard
    with_refresh_fixture do |owners, owner, sent|
      LabInventory.define_singleton_method(:fput) { |command| owner.emit(command) }
      LabInventory.handle_request('charges refresh')
      assert_equal ['inventory enhancive list'], sent
      owners[:combat] = 'bigshot'
      LabInventory.handle_request('charges refresh')
      assert_equal ['inventory enhancive list'], sent
      refute LabInventory.inventory_lane_active?
    ensure
      LabInventory.singleton_class.send(:remove_method, :fput)
    end
  end

  def setup
    database = LabInventory.instance_variable_get(:@database)
    database.close if database
    LabInventory.instance_variable_set(:@database, nil)
    FileUtils.rm_rf(DATA_DIR)
    FileUtils.mkdir_p(DATA_DIR)
    LabInventory.instance_variable_set(:@dirty, false)
    LabInventory.instance_variable_set(:@loaded, true)
    LabInventory.instance_variable_set(:@dossiers, {})
    LabInventory.instance_variable_set(:@session_index, {})
    LabInventory.instance_variable_set(:@locations, {})
    LabInventory.instance_variable_set(:@container_seen_at, {})
    LabInventory.instance_variable_set(:@session_id, 'test-session')
    LabInventory.instance_variable_set(:@capture, nil)
    LabInventory.instance_variable_set(:@focused_local_id, nil)
    LabInventory.instance_variable_set(:@transcript, nil)
    XMLData.character_name = 'Testwarrior'
    GameObj.inventory = []
    GameObj.container_map = {}
    GameObj.right = nil
    GameObj.left = nil
  end

  def legacy_dossier(id, full_name, facts: [], observations: [])
    identity = { 'type' => '', 'noun' => full_name.split.last, 'name' => full_name, 'full_name' => full_name }
    {
      'id' => id, 'identity' => identity, 'fingerprint' => LabInventory.fingerprint(identity),
      'facts' => facts, 'observations' => observations,
      'created_at' => '2025-01-01T00:00:00.000Z', 'updated_at' => '2025-01-01T00:00:00.000Z'
    }
  end

  def write_legacy_inventory(character, dossiers)
    path = LabInventory.legacy_data_path(character)
    FileUtils.mkdir_p(File.dirname(path))
    File.write(path, JSON.generate('schema_version' => 1, 'game' => 'GSIV', 'character' => character, 'dossiers' => dossiers.to_h { |dossier| [dossier['id'], dossier] }))
  end

  def herb(id)
    FakeInventoryObject.new(id.to_s, 'leaf', 'some acantha leaf', 'some acantha leaf')
  end

  def seed_linked_dossier(item, local_id)
    dossier = LabInventory.blank_dossier(local_id, LabInventory.object_identity(item))
    dossier['last_game_id'] = item.id
    LabInventory.instance_variable_get(:@dossiers)[local_id] = dossier
    LabInventory.instance_variable_get(:@session_index)[item.id] = local_id
    dossier
  end

  def test_identical_objects_remain_separate_items
    first = herb(101)
    second = herb(102)
    GameObj.inventory = [first, second]

    result = LabInventory.query('acantha')

    assert_equal 2, result['item_count']
    refute_equal result['items'][0]['id'], result['items'][1]['id']
  end

  def test_exact_dose_measurement_is_reused_and_adjusted
    item = herb(101)
    GameObj.inventory = [item]
    LabInventory.sync_live_state

    assert LabInventory.record_dose(item, 10, source: 'test measurement')
    assert_equal 10, LabInventory.dose_for(item)

    LabInventory.adjust_dose(item.id, -1)
    assert_equal 9, LabInventory.query('acantha')['known_exact_doses']
  end

  def test_assessment_facts_are_attached_to_the_unique_item_dossier
    maul = FakeInventoryObject.new(
      '201', 'maul', 'practice maul',
      'a plain iron practice maul'
    )
    GameObj.inventory = [maul]
    LabInventory.sync_live_state

    LabInventory.handle_input('assess my maul')
    LabInventory.observe(
      'Careful examination indicates the practice maul has a base strength of 50 and a base durability of 100.  ' \
      'You also determine the current integrity of the practice maul to be at 100.0%.'
    )
    LabInventory.observe(
      'It also has a combat effectiveness rating of 2 points of Critical weighting.'
    )

    facts = LabInventory.query('practice maul')['items'].first['facts']
    assert facts.any? { |fact| fact['field'] == 'base_strength' && fact['value'] == 50 }
    assert facts.any? { |fact| fact['field'] == 'base_durability' && fact['value'] == 100 }
    assert facts.any? { |fact| fact['field'] == 'integrity_percent' && fact['value'] == 100.0 }
    assert facts.any? { |fact| fact['field'] == 'critical_cer' && fact['value'] == 2 }
  end

  def test_bounded_transcript_retains_narrative_item_lore
    necklace = FakeInventoryObject.new('301', 'necklace', 'silver necklace', 'an ancient silver necklace')
    GameObj.inventory = [necklace]
    LabInventory.sync_live_state

    LabInventory.handle_request('learn necklace | bard loresong by Testbard')
    LabInventory.observe('A vision of a forgotten queen crossing the frozen sea fills your thoughts.')
    LabInventory.handle_request('learn done')

    dossier = LabInventory.resolve('necklace').first
    assert dossier['observations'].any? do |observation|
      observation['text'].include?('forgotten queen') && observation['source'] == 'bard loresong by Testbard'
    end
  end

  def test_reverse_lookup_finds_an_item_by_its_effect
    crystal = FakeInventoryObject.new('401', 'crystal', 'red crystal', 'a pulsing red crystal')
    GameObj.inventory = [crystal]
    LabInventory.sync_live_state
    assert LabInventory.remember(
      crystal,
      'effect',
      'refills stamina to maximum',
      source: 'player item knowledge',
      confidence: 'player-reported'
    )

    matches = LabInventory.search('stamina', current_only: true)

    assert_equal 1, matches.length
    assert_equal 'a pulsing red crystal', matches.first.dig('identity', 'full_name')
  end

  def test_item_property_adapter_records_magic_and_profession_findings
    necklace = FakeInventoryObject.new('501', 'necklace', 'gold necklace', 'an etched gold necklace')
    GameObj.inventory = [necklace]
    LabInventory.sync_live_state
    LabInventory.handle_input('recall my necklace')
    LabInventory.observe('It imparts a bonus of +25 more than usual.')
    LabInventory.observe('It provides a boost of 5 to Constitution.')
    LabInventory.observe('It provides a boost of 5 to Constitution Bonus.')
    LabInventory.observe('It could be activated by rubbing it.')
    LabInventory.observe('It has been ensorcelled 3 times.')

    facts = LabInventory.query('gold necklace')['items'].first['facts']
    assert facts.any? { |fact| fact['field'] == 'enchant_bonus' && fact['value'] == 25 }
    assert facts.any? { |fact| fact['field'] == 'enhancive.constitution' && fact['value'] == 5 }
    assert facts.any? { |fact| fact['field'] == 'enhancive.constitution_bonus' && fact['value'] == 5 }
    assert facts.any? { |fact| fact['field'] == 'activation' && fact['value'] == 'RUB' }
    assert facts.any? { |fact| fact['field'] == 'ensorcell_tier' && fact['value'] == 3 }
  end

  def test_id_targeted_read_attaches_scroll_spells_to_one_duplicate_item
    first = FakeInventoryObject.new('801', 'scroll', 'aged scroll', 'an aged scroll')
    second = FakeInventoryObject.new('802', 'scroll', 'aged scroll', 'an aged scroll')
    # Random digest IDs occasionally contain another object's numeric ID.
    # Make that collision deterministic instead of relying on process timing.
    seed_linked_dossier(first, 'item-0000080200000000')
    seed_linked_dossier(second, 'item-1111111111111111')
    GameObj.container_map = { '900' => [first, second] }
    LabInventory.sync_live_state

    LabInventory.handle_input('read #802')
    LabInventory.observe('On the aged scroll you see')
    LabInventory.observe('(215) Heroism in vibrant ink')
    LabInventory.observe('(303) Prayer of Protection')
    LabInventory.observe('<prompt>')

    first_dossier = LabInventory.resolve('#801', current_only: true).first
    second_dossier = LabInventory.resolve('#802', current_only: true).first
    assert_empty first_dossier['facts']
    assert second_dossier['facts'].any? { |fact|
      fact['field'] == 'scroll.spell.215' && fact['value'] == 'Heroism'
    }
    assert second_dossier['facts'].any? { |fact|
      fact['field'] == 'scroll.vibrant_ink.215' && fact['value'] == true
    }
    assert second_dossier['facts'].any? { |fact|
      fact['field'] == 'scroll.spell.303' && fact['value'] == 'Prayer of Protection'
    }
    assert second_dossier['facts'].any? { |fact|
      fact['field'] == 'item_role' && fact['value'] == 'spell-bearing readable object'
    }
    assert second_dossier['facts'].any? { |fact|
      fact['field'] == 'restriction' && fact['value'] == 'never auto-sell'
    }
  end

  def test_numeric_object_selectors_never_match_substrings_or_item_names
    target = FakeInventoryObject.new('802', 'scroll', 'aged scroll', 'an aged scroll')
    longer_id = FakeInventoryObject.new('1802', 'scroll', 'aged scroll', 'an aged scroll')
    numbered_name = FakeInventoryObject.new('901', 'scroll', 'scroll marked 802', 'a scroll marked 802')
    GameObj.inventory = [target, longer_id, numbered_name]
    seed_linked_dossier(target, 'item-1111111111111111')
    seed_linked_dossier(longer_id, 'item-0000080200000000')
    LabInventory.sync_live_state

    %w[#802 802].each do |query|
      assert_equal ['802'], LabInventory.resolve(query, current_only: true).map { |dossier| dossier['last_game_id'] }
    end
    assert_empty LabInventory.resolve('#80', current_only: true)
    assert_equal 2, LabInventory.resolve('aged scroll', current_only: true).length
    assert_equal ['1802'], LabInventory.resolve('item-0000080200000000', current_only: true).map { |dossier| dossier['last_game_id'] }
  end

  def test_repeated_live_scroll_spell_lines_remain_distinct_slots
    scroll = FakeInventoryObject.new('850', 'scroll', 'prayerbook', 'a plain leather prayerbook')
    GameObj.inventory = [scroll]
    LabInventory.sync_live_state

    LabInventory.handle_input('read #850')
    3.times { LabInventory.observe('(401) Elemental Defense I') }
    3.times { LabInventory.observe('(406) Elemental Defense II') }
    LabInventory.observe('<prompt>')

    facts = LabInventory.resolve('#850', current_only: true).first['facts']
    assert_equal 3, facts.count { |fact| fact['field'] == 'scroll.spell.401' && fact['value'] == 'Elemental Defense I' }
    assert_equal 3, facts.count { |fact| fact['field'] == 'scroll.spell.406' && fact['value'] == 'Elemental Defense II' }
  end

  def test_successful_reread_reconciles_slots_without_losing_repeated_spells
    scroll = FakeInventoryObject.new('851', 'scroll', 'prayerbook', 'a plain leather prayerbook')
    GameObj.inventory = [scroll]
    LabInventory.sync_live_state

    2.times do
      LabInventory.handle_input('read #851')
      3.times { LabInventory.observe('(401) Elemental Defense I') }
      LabInventory.observe('<prompt>')
    end

    facts = LabInventory.resolve('#851', current_only: true).first['facts']
    assert_equal 3, facts.count { |fact| fact['field'] == 'scroll.spell.401' && fact['value'] == 'Elemental Defense I' }
  end

  def test_player_can_mark_exact_scroll_id_used_or_fresh
    scroll = FakeInventoryObject.new('860', 'scroll', 'aged scroll', 'an aged scroll')
    GameObj.inventory = [scroll]
    LabInventory.sync_live_state

    LabInventory.handle_request('scroll #860 used')
    used = LabInventory.query('#860')['items'].first['scroll_infusion']
    assert_equal 'used_not_unlockable', used['state']
    assert_equal false, used['can_unlock']
    assert_equal false, used['can_recharge']

    LabInventory.handle_request('scroll #860 fresh')
    fresh = LabInventory.query('#860')['items'].first['scroll_infusion']
    assert_equal 'fresh_unlock_candidate', fresh['state']
    assert_equal true, fresh['can_unlock']
  end

  def test_detection_rune_assessment_preserves_duplicate_slots_and_potential
    scroll = FakeInventoryObject.new('870', 'prayerbook', 'crimson prayerbook', 'a plain leather prayerbook')
    stone = FakeInventoryObject.new('871', 'stone', 'smooth stone', 'a smooth stone')
    GameObj.right = scroll
    GameObj.left = stone
    LabInventory.sync_live_state

    LabInventory.handle_input('wave #871 at #870')
    LabInventory.observe('As the stone passes over the scroll, you sense:')
    LabInventory.observe('(401) Elemental Defense I with many charges remaining and the potential to add a number of charges.')
    LabInventory.observe('(401) Elemental Defense I with very many charges remaining.')
    LabInventory.observe('(406) Elemental Defense II with a number of charges remaining.')
    LabInventory.observe('<prompt>')

    item = LabInventory.query('#870')['items'].first
    status = item['scroll_infusion']
    assert_equal 'unlocked_rechargeable', status['state']
    assert_equal false, status['can_unlock']
    assert_equal true, status['can_recharge']
    slots = status.dig('assessment', 'slots')
    assert_equal [401, 401, 406], slots.map { |slot| slot['spell_number'] }
    assert_equal 'a number of', slots.first['potential']
    refute slots[1].key?('potential')
  end

  def test_detection_rune_assessment_accepts_live_charge_wording_without_potential
    scroll = FakeInventoryObject.new('875', 'paper', 'yellowed paper', 'a piece of yellowed paper')
    stone = FakeInventoryObject.new('876', 'stone', 'smooth stone', 'a smooth stone')
    GameObj.right = scroll
    GameObj.left = stone
    LabInventory.sync_live_state

    LabInventory.handle_input('wave #876 at #875')
    LabInventory.observe('As the stone passes over the scroll, you sense:')
    LabInventory.observe('(903) Minor Water with a number of charges remaining.')
    LabInventory.observe('(916) Invisibility with a number of charges remaining.')
    LabInventory.observe('(401) Elemental Defense I with a couple charges remaining.')
    LabInventory.observe('(912) Call Wind with a number of charges remaining.')
    LabInventory.observe('(408) Disarm with a number of charges remaining.')
    LabInventory.observe('(414) Elemental Defense III with a number of charges remaining.')
    LabInventory.observe('<prompt>')

    status = LabInventory.query('#875')['items'].first['scroll_infusion']
    assert_equal 'unknown', status['state']
    assert_nil status['can_unlock']
    assert_equal false, status['can_recharge']
    slots = status.dig('assessment', 'slots')
    assert_equal [903, 916, 401, 912, 408, 414], slots.map { |slot| slot['spell_number'] }
    assert_equal 'a couple', slots[2]['charges']
    assert slots.none? { |slot| slot.key?('potential') }
  end

  def test_detection_rune_assessment_accepts_spell_knowledge_time_wording
    scroll = FakeInventoryObject.new('877', 'scroll', 'tattered scroll', 'a tattered scroll')
    stone = FakeInventoryObject.new('878', 'stone', 'smooth stone', 'a smooth stone')
    GameObj.right = scroll
    GameObj.left = stone
    LabInventory.sync_live_state

    LabInventory.handle_input('wave #878 at #877')
    LabInventory.observe('As the stone passes over the scroll, you sense:')
    LabInventory.observe('(1111) Limb Scar Repair with about 5 minutes of time remaining.')
    LabInventory.observe('(207) Purify Air with about 5 minutes of time remaining.')
    LabInventory.observe('(215) Heroism with about 5 minutes of time remaining.')
    LabInventory.observe('(113) UnDisease with about 5 minutes of time remaining.')
    LabInventory.observe('<prompt>')

    status = LabInventory.query('#877')['items'].first['scroll_infusion']
    assert_equal 'unknown', status['state']
    assert_nil status['can_unlock']
    assert_equal false, status['can_recharge']
    slots = status.dig('assessment', 'slots')
    assert_equal [1111, 207, 215, 113], slots.map { |slot| slot['spell_number'] }
    assert_equal ['about 5 minutes'] * 4, slots.map { |slot| slot['duration'] }
    assert slots.none? { |slot| slot.key?('charges') }
  end

  def test_failed_unlock_remains_ambiguous_without_freshness_evidence
    scroll = FakeInventoryObject.new('880', 'scroll', 'aged scroll', 'an aged scroll')
    stone = FakeInventoryObject.new('881', 'stone', 'smooth stone', 'a smooth stone')
    GameObj.right = scroll
    GameObj.left = stone
    LabInventory.sync_live_state

    LabInventory.handle_input('wave #881 at #880')
    LabInventory.observe('The smooth stone shakes slightly, but nothing happens.')
    LabInventory.observe('<prompt>')

    status = LabInventory.query('#880')['items'].first['scroll_infusion']
    assert_equal 'unlock_attempt_failed', status['state']
    assert_nil status['can_unlock']
    assert_equal false, status['can_recharge']
  end

  def test_scroll_mutation_rebinds_new_item_id_to_precommand_dossier
    scroll = FakeInventoryObject.new('890', 'scroll', 'aged scroll', 'an aged scroll')
    stone = FakeInventoryObject.new('891', 'stone', 'smooth stone', 'a smooth stone')
    GameObj.right = scroll
    GameObj.left = stone
    LabInventory.sync_live_state
    dossier_id = LabInventory.resolve('#890', current_only: true).first['id']

    LabInventory.handle_input('wave #891 at #890')
    GameObj.right = FakeInventoryObject.new(
      '892', 'scroll', 'altered aged scroll', 'an altered aged scroll'
    )
    LabInventory.observe('The smooth stone vibrates and begins to glow.  You sense that the scroll has somehow been altered.')
    LabInventory.observe('<prompt>')

    rebound = LabInventory.resolve('#892', current_only: true)
    assert_equal 1, rebound.length
    assert_equal dossier_id, rebound.first['id']
    assert_equal '892', rebound.first['last_game_id']
    assert_equal 'unlocked_unassessed', LabInventory.query('#892')['items'].first.dig('scroll_infusion', 'state')
  end

  def test_persisted_snapshot_drops_invalidated_doses_and_replaced_non_scroll_facts
    leaf = herb(901)
    GameObj.inventory = [leaf]
    LabInventory.sync_live_state
    assert LabInventory.record_dose(leaf, 10)
    assert LabInventory.remember(leaf, 'effect', 'old effect', source: 'test')
    LabInventory.persist

    LabInventory.invalidate_dose(leaf.id, 'test invalidation')
    assert LabInventory.remember(leaf, 'effect', 'new effect', source: 'test')
    LabInventory.persist
    database = LabInventory.instance_variable_get(:@database)
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM doses WHERE dossier_id = ? AND active = 0', [LabInventory.resolve('#901').first['id']]).to_i
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND field = ? AND active = 0', [LabInventory.resolve('#901').first['id'], 'effect']).to_i
    fact_count = database.get_first_value('SELECT COUNT(*) FROM facts').to_i
    observation_count = database.get_first_value('SELECT COUNT(*) FROM observations').to_i
    LabInventory.instance_variable_set(:@dirty, true)
    LabInventory.persist
    assert_equal fact_count, database.get_first_value('SELECT COUNT(*) FROM facts').to_i
    assert_equal observation_count, database.get_first_value('SELECT COUNT(*) FROM observations').to_i

    database.close
    LabInventory.instance_variable_set(:@database, nil)
    LabInventory.instance_variable_set(:@dossiers, {})
    LabInventory.instance_variable_set(:@session_index, {})
    LabInventory.instance_variable_set(:@locations, {})
    LabInventory.instance_variable_set(:@loaded, false)
    LabInventory.load_state

    dossier = LabInventory.resolve('#901').first
    assert_nil dossier['dose_measurement']
    assert dossier['facts'].any? { |fact| fact['field'] == 'effect' && fact['value'] == 'new effect' }
    refute dossier['facts'].any? { |fact| fact['field'] == 'effect' && fact['value'] == 'old effect' }
  end

  def test_one_changed_dossier_does_not_rewrite_unrelated_fact_rows
    changed, unchanged = herb(910), herb(911)
    GameObj.inventory = [changed, unchanged]
    [changed, unchanged].each { |item| LabInventory.remember(item, 'effect', 'old effect', source: 'test') }
    LabInventory.persist
    database = LabInventory.send(:database)
    unchanged_id = LabInventory.resolve('#911').first['id']
    database.execute_batch(<<~SQL)
      CREATE TEMP TABLE fact_writes (dossier_id TEXT);
      CREATE TEMP TRIGGER count_fact_updates AFTER UPDATE ON facts BEGIN
        INSERT INTO fact_writes VALUES (NEW.dossier_id);
      END;
    SQL

    LabInventory.remember(changed, 'effect', 'new effect', source: 'test')
    LabInventory.persist

    assert_operator database.get_first_value('SELECT COUNT(*) FROM fact_writes').to_i, :>, 0
    assert_equal 0, database.get_first_value('SELECT COUNT(*) FROM fact_writes WHERE dossier_id = ?', [unchanged_id]).to_i
    assert_equal 2, database.get_first_value('SELECT COUNT(*) FROM session_locations WHERE session_id = ?', ['test-session']).to_i
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND active = 1', [unchanged_id]).to_i
  end

  def test_failed_persist_retries_changes_after_transaction_rollback
    leaf = herb(912)
    GameObj.inventory = [leaf]
    LabInventory.record_dose(leaf, 10)
    LabInventory.remember(leaf, 'effect', 'old effect', source: 'test')
    LabInventory.persist
    database = LabInventory.send(:database)
    dossier_id = LabInventory.resolve('#912').first['id']
    database.execute_batch(<<~SQL)
      CREATE TEMP TRIGGER fail_location_save BEFORE INSERT ON session_locations BEGIN
        SELECT RAISE(ABORT, 'isolated persistence failure');
      END;
    SQL
    LabInventory.adjust_dose('912', -1)
    LabInventory.remember(leaf, 'effect', 'new effect', source: 'test')
    LabInventory.persist

    assert LabInventory.instance_variable_get(:@dirty)
    assert_equal 10, database.get_first_value('SELECT value FROM doses WHERE dossier_id = ?', [dossier_id])
    assert_equal '"old effect"', database.get_first_value('SELECT value_json FROM facts WHERE dossier_id = ? AND active = 1', [dossier_id])
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM session_locations').to_i

    database.execute('DROP TRIGGER fail_location_save')
    LabInventory.persist

    refute LabInventory.instance_variable_get(:@dirty)
    assert_equal 9, database.get_first_value('SELECT value FROM doses WHERE dossier_id = ?', [dossier_id])
    assert_equal '"new effect"', database.get_first_value('SELECT value_json FROM facts WHERE dossier_id = ? AND active = 1', [dossier_id])
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND active = 0', [dossier_id]).to_i
  end

  def test_dose_only_change_is_saved_without_a_dossier_timestamp_change
    leaf = herb(913)
    GameObj.inventory = [leaf]
    LabInventory.record_dose(leaf, 10)
    LabInventory.persist
    dossier = LabInventory.resolve('#913').first
    timestamp = dossier['updated_at']

    LabInventory.adjust_dose('913', -1)
    assert_equal timestamp, dossier['updated_at']
    LabInventory.persist

    database = LabInventory.send(:database)
    assert_equal 9, database.get_first_value('SELECT value FROM doses WHERE dossier_id = ?', [dossier['id']])
  end

  def test_mark_and_registration_responses_are_saved_from_their_xml_item_anchor
    necklace = FakeInventoryObject.new('601', 'necklace', 'silver necklace', 'an ancient silver necklace')
    GameObj.inventory = [necklace]
    LabInventory.sync_live_state

    LabInventory.observe('<a exist="601" noun="necklace">an ancient silver necklace</a> is marked as unsellable.')
    LabInventory.observe('The <a exist="601" noun="necklace">ancient silver necklace</a> was last registered to you.')
    LabInventory.observe('You carefully inspect your <a exist="601" noun="necklace">ancient silver necklace</a>. You remember that you registered this item between 3 and 4 years ago.')

    facts = LabInventory.query('silver necklace')['items'].first['facts']
    assert facts.any? { |fact| fact['field'] == 'marked_unsellable' && fact['value'] == true && fact['source'] == 'MARK STATUS' }
    assert facts.any? { |fact| fact['field'] == 'registered' && fact['value'] == true }
    assert facts.any? { |fact| fact['field'] == 'registered_owner' && fact['value'] == 'Testwarrior' }
    assert facts.any? { |fact| fact['field'] == 'registration_age' && fact['value'] == 'between 3 and 4 years ago' }
  end

  def test_negative_protection_responses_are_saved_only_when_one_item_is_anchored
    necklace = FakeInventoryObject.new('701', 'necklace', 'silver necklace', 'an ancient silver necklace')
    ring = FakeInventoryObject.new('702', 'ring', 'silver ring', 'an ancient silver ring')
    GameObj.inventory = [necklace, ring]
    LabInventory.sync_live_state

    LabInventory.observe('<a exist="701">an ancient silver necklace</a> is not marked as unsellable.  No recorded registration information exists for it.')
    LabInventory.observe('<a exist="701">necklace</a> and <a exist="702">ring</a> are marked as unsellable.')

    necklace_facts = LabInventory.query('silver necklace')['items'].first['facts']
    assert necklace_facts.any? { |fact| fact['field'] == 'marked_unsellable' && fact['value'] == false }
    assert necklace_facts.any? { |fact| fact['field'] == 'registered' && fact['value'] == false }
    refute necklace_facts.any? { |fact| fact['field'] == 'marked_unsellable' && fact['value'] == true }
    assert_empty LabInventory.query('silver ring')['items'].first['facts']
  end

  def test_account_database_imports_characters_idempotently_and_recovers_spell_slots
    observation = ->(line) { { 'text' => line, 'source' => 'read #801', 'observed_at' => '2025-01-01T01:00:00.000Z' } }
    prayerbook = legacy_dossier(
      'testwarrior-prayerbook', 'a plain leather prayerbook',
      facts: [
        { 'field' => 'scroll.spell.401', 'value' => 'Elemental Defense I', 'source' => 'read #801', 'confidence' => 'observed', 'observed_at' => '2025-01-01T01:00:00.000Z' },
        { 'field' => 'scroll.spell.406', 'value' => 'Elemental Defense II', 'source' => 'read #801', 'confidence' => 'observed', 'observed_at' => '2025-01-01T01:00:00.000Z', 'evidence' => '(406) Elemental Defense II' }
      ],
      observations: ['(401) Elemental Defense I', '(401) Elemental Defense I', '(401) Elemental Defense I', '(406) Elemental Defense II', '(406) Elemental Defense II', '(406) Elemental Defense II'].map(&observation)
    )
    vellum = legacy_dossier(
      'testwarrior-vellum', 'a rolled vellum',
      facts: [{ 'field' => 'scroll.spell.120', 'value' => 'Lesser Shroud', 'source' => 'read #802', 'confidence' => 'observed', 'observed_at' => '2025-01-01T01:00:00.000Z' }],
      observations: Array.new(6) { observation.call('(120) Lesser Shroud') }
    )
    duplicate_one = legacy_dossier('testmage-ring-one', 'a black ring')
    duplicate_two = legacy_dossier('testmage-ring-two', 'a black ring')
    write_legacy_inventory('Testwarrior', [prayerbook, vellum])
    write_legacy_inventory('Testmage', [duplicate_one, duplicate_two])
    malformed_path = LabInventory.legacy_data_path('Broken')
    FileUtils.mkdir_p(File.dirname(malformed_path))
    File.write(malformed_path, '{not json')

    LabInventory.instance_variable_set(:@loaded, false)
    LabInventory.load_state
    database = LabInventory.send(:database)
    assert_equal 2, database.get_first_value('SELECT COUNT(*) FROM characters').to_i
    assert_equal 2, database.get_first_value('SELECT COUNT(*) FROM dossiers WHERE character_id = (SELECT id FROM characters WHERE name = ?)', ['Testmage']).to_i
    assert_equal 3, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND field = ?', ['testwarrior-prayerbook', 'scroll.spell.401']).to_i
    assert_equal 3, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND field = ?', ['testwarrior-prayerbook', 'scroll.spell.406']).to_i
    assert_equal 6, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND field = ?', ['testwarrior-vellum', 'scroll.spell.120']).to_i
    assert_equal 'read #801', database.get_first_value('SELECT source FROM facts WHERE dossier_id = ? AND field = ? ORDER BY ordinal LIMIT 1', ['testwarrior-prayerbook', 'scroll.spell.401'])
    assert_equal 'failed', database.get_first_value('SELECT status FROM legacy_imports WHERE source_path = ?', [malformed_path])

    LabInventory.instance_variable_set(:@loaded, false)
    LabInventory.load_state
    assert_equal 6, database.get_first_value('SELECT COUNT(*) FROM facts WHERE dossier_id = ? AND field = ?', ['testwarrior-vellum', 'scroll.spell.120']).to_i

    assert LabInventory.remember('testwarrior-prayerbook', 'effect', 'DB-only fact', source: 'runtime')
    LabInventory.persist
    write_legacy_inventory('Testwarrior', [prayerbook.merge('facts' => [{ 'field' => 'effect', 'value' => 'legacy stale fact', 'source' => 'legacy', 'confidence' => 'observed', 'observed_at' => '2025-01-01T02:00:00.000Z' }]), vellum])
    LabInventory.instance_variable_set(:@loaded, false)
    LabInventory.load_state
    facts = LabInventory.resolve('testwarrior-prayerbook').first['facts']
    assert facts.any? { |fact| fact['field'] == 'effect' && fact['value'] == 'DB-only fact' }
    refute facts.any? { |fact| fact['field'] == 'effect' && fact['value'] == 'legacy stale fact' }
  end

  def test_binary_live_strings_reuse_text_character_and_dossier_identity
    staff = legacy_dossier('testmage-staff', 'a plain oak runestaff')
    write_legacy_inventory('Testmage', [staff])
    XMLData.character_name = 'Testmage'.b
    GameObj.inventory = [
      FakeInventoryObject.new(
        '901'.b, 'runestaff'.b, 'a plain oak runestaff'.b,
        'a plain oak runestaff'.b
      )
    ]

    LabInventory.instance_variable_set(:@loaded, false)
    LabInventory.load_state
    LabInventory.sync_live_state
    LabInventory.persist

    database = LabInventory.send(:database)
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM characters').to_i
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM dossiers').to_i
    assert_equal 'text', database.get_first_value('SELECT DISTINCT typeof(name) FROM characters')
    assert_equal 'text', database.get_first_value('SELECT DISTINCT typeof(id) FROM dossiers')
    assert_equal 'text', database.get_first_value('SELECT DISTINCT typeof(fingerprint) FROM dossiers')
  end

  def test_close_database_releases_the_sqlite_handle
    database = LabInventory.send(:database)
    refute database.closed?

    LabInventory.close_database

    assert database.closed?
    assert_nil LabInventory.instance_variable_get(:@database)
    refute LabInventory.instance_variable_get(:@loaded)
  end

  def test_new_digest_dossier_ids_are_persisted_as_sqlite_text
    identity = {
      'type' => ''.b, 'noun' => 'gem'.b,
      'name' => 'an unfamiliar gem'.b, 'full_name' => 'an unfamiliar gem'.b
    }
    dossier = LabInventory.blank_dossier('item-binary-digest'.b, identity)
    dossier['last_game_id'] = '902'.b
    LabInventory.instance_variable_set(:@dossiers, { dossier['id'] => dossier })
    LabInventory.instance_variable_set(:@dirty, true)
    LabInventory.persist

    database = LabInventory.send(:database)
    assert_equal 1, database.get_first_value('SELECT COUNT(*) FROM dossiers').to_i
    assert_equal 'text', database.get_first_value('SELECT typeof(id) FROM dossiers')
    assert_equal 'text', database.get_first_value('SELECT typeof(fingerprint) FROM dossiers')
    assert_equal 'text', database.get_first_value('SELECT typeof(last_game_id) FROM dossiers')
  end
end
