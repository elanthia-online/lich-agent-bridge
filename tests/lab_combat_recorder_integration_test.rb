# frozen_string_literal: true

# Optional cross-repository test against reviewed source, not a running install.
require 'minitest/autorun'
require 'tmpdir'
require 'set'
require_relative '../lich/lab-combat-report'

if ENV['LAB_TEST_LICH_CORE']
  LIB_DIR = File.join(File.expand_path(ENV.fetch('LAB_TEST_LICH_CORE')), 'lib')
  require File.join(LIB_DIR, 'gemstone/combat/processor')
  require File.join(LIB_DIR, 'gemstone/combat/recorder')
end

class CombatRecorderIntegrationTest < Minitest::Test
  def test_saved_attacks_with_rejected_sources_have_distinct_diagnostics
    skip 'set LAB_TEST_LICH_CORE to the matching reviewed core checkout' unless ENV['LAB_TEST_LICH_CORE']
    Dir.mktmpdir('lab-recorder-rejections') do |dir|
      path = File.join(dir, 'combat_stats.db')
      core = Lich::Gemstone::Combat
      core::Recorder.new(path, character: 'Tester').close
      recorder = core::Recorder.new(path, character: 'Tester', idle_timeout: 300)
      recorder.subscribe!
      context = { connection_id: 1, game: 'GS3', character: 'Tester', room_epoch: 7 }
      capture = LabCombatReport::Capture.new(character: 'Tester', context: context,
                                            database: path, observers: core::Observers)
      [nil, context.merge(connection_id: 2, sequence: 1, received_at: 10.0)].each do |source|
        core::Observers.emit(:attack, {
          name: :fixture, target: { id: 123, noun: 'rat', name: 'a rat' },
          source: source, hits: [{ damage: 87 }, { damage: 60 }]
        })
      end
      result = capture.report(safe: true, trials: [])
      db = SQLite3::Database.new(path, readonly: true)
      assert_equal 2, db.get_first_value('SELECT COUNT(*) FROM attacks')
      assert_equal 'no_attributable_recorder_receipts', result[:reason]
      assert_equal({ received: 2, accepted: 0, missing_source: 1, binding_mismatch: 1 }, result[:receipt_counts])
      assert_empty result[:trials]
    ensure
      capture&.close
      db&.close
      recorder&.close
    end
  end

  def test_real_core_commit_receipt_to_read_only_trial_report
    skip 'set LAB_TEST_LICH_CORE to the matching reviewed core checkout' unless ENV['LAB_TEST_LICH_CORE']
    Dir.mktmpdir('lab-recorder-contract') do |dir|
      path = File.join(dir, 'combat_stats.db')
      core = Lich::Gemstone::Combat
      recorder = core::Recorder.new(path, character: 'Tester')
      recorder.start_session(character: 'Tester')
      recorder.subscribe!
      context = { connection_id: 1, game: 'GS3', character: 'Tester', room_epoch: 7 }
      capture = LabCombatReport::Capture.new(character: 'Tester', context: context,
                                            database: path, observers: core::Observers)
      core::Observers.emit(:attack, {
        name: :fixture, target: { id: 123, noun: 'rat', name: 'a rat' },
        source: context.merge(sequence: 1, received_at: 10.0),
        hits: [{ damage: 50 }], resolutions: [{ type: :cs_td, cs: 200, td: 100, result: 150 }],
        flares: [{ name: :fire, hits: [{ damage: 7 }] }]
      })
      result = capture.report(safe: true, trials: [{ index: 1, routine: 'a', target_id: '123',
        started_at: 9.0, finished_at: 11.0, recording_context: context }])
      assert_equal 'observed', result[:status], result.inspect
      trial = result[:trials].first
      assert_equal [1, 50, 7], trial.values_at(:own_attack_records, :direct_damage, :flare_damage)
      assert_equal({ 'cs_td' => [100, 100] }, trial[:observed_defenses])
      assert_equal [{ session_id: recorder.session_id, attack_id: 1 }], trial[:attack_refs]
      # Unsubscribing LAB must not stop the player's recorder.
      core::Observers.emit(:attack, { name: :fixture, target: { id: 123 }, hits: [{ damage: 2 }] })
      db = SQLite3::Database.new(path, readonly: true)
      assert_equal 2, db.get_first_value('SELECT COUNT(*) FROM attacks')
    ensure
      capture&.close
      db&.close
      recorder&.close
    end
  end
end
