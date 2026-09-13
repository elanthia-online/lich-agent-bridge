# frozen_string_literal: true

require 'minitest/autorun'
require 'tmpdir'
require 'sqlite3'
require_relative '../lich/lab-combat-report'

class LabCombatReportTest < Minitest::Test
  class Observers
    attr_reader :handlers, :removed
    def initialize
      @handlers, @removed = [], []
    end
    def on(_type, &block)
      @handlers << block
      block
    end
    def off(handler)
      @removed << handler
      @handlers.delete(handler)
    end
    def emit(receipt)
      @handlers.dup.each { |h| h.call(:recorded_attack, receipt) }
    end
  end

  def setup
    @dir = Dir.mktmpdir('lab-combat-test')
    @path = File.join(@dir, 'combat_stats.db')
    @db = SQLite3::Database.new(@path)
    @db.execute_batch(<<~SQL)
      CREATE TABLE sessions(id INTEGER PRIMARY KEY,character TEXT);
      CREATE TABLE creatures(id INTEGER PRIMARY KEY,exist_id INTEGER);
      CREATE TABLE attacks(id INTEGER PRIMARY KEY,session_id INTEGER,creature_id INTEGER,
        inbound INTEGER,foreign_caster INTEGER,unowned INTEGER,target_kind TEXT,attacker_exist_id INTEGER);
      CREATE TABLE hits(attack_id INTEGER,creature_id INTEGER,flare_id INTEGER,damage INTEGER);
      CREATE TABLE resolutions(attack_id INTEGER,flare_id INTEGER,type TEXT,defender_stat INTEGER);
      INSERT INTO sessions VALUES(1,'Tester');
      INSERT INTO creatures VALUES(1,123);
      INSERT INTO attacks VALUES(1,1,1,0,0,0,'creature',NULL);
      INSERT INTO hits VALUES(1,1,NULL,50),(1,1,1,7);
      INSERT INTO resolutions VALUES(1,NULL,'cs_td',100);
      INSERT INTO attacks VALUES(2,1,1,0,1,0,'foreign',NULL);
      INSERT INTO hits VALUES(2,1,NULL,20);
      INSERT INTO attacks VALUES(3,1,NULL,1,0,0,'self',123);
      INSERT INTO hits VALUES(3,NULL,NULL,3);
    SQL
    @context = { connection_id: 1, game: 'GS3', character: 'Tester', room_epoch: 7 }
    @observers = Observers.new
    @capture = capture
  end

  def teardown
    @capture.close
    @db.close
    FileUtils.remove_entry(@dir)
  end

  def capture(**extra)
    LabCombatReport::Capture.new(character: 'Tester', context: @context, database: @path,
                                observers: @observers, **extra)
  end

  def receipt(id = 1, **extra)
    stat = File.stat(@path)
    { protocol: 1, recorder_id: 'a' * 32, database: File.realpath(@path),
      file_identity: [stat.dev, stat.ino], session_id: 1, attack_id: id,
      source: @context.merge(sequence: id, received_at: 10.0) }.merge(extra)
  end

  def trial(**extra)
    { index: 1, routine: 'a', target_id: '123', outcome: 'dead',
      recording_context: @context, started_at: 9.0, finished_at: 11.0,
      elapsed_seconds: 2.0, final_resources: { mana: 90 } }.merge(extra)
  end

  def report(trials: [trial], safe: true)
    @capture.report(trials: trials, safe: safe)
  end

  def test_exact_rows_separate_own_foreign_flare_and_incoming_damage
    (1..3).each { |id| @observers.emit(receipt(id)) }
    result = report
    assert_equal 'observed', result[:status]
    row = result[:trials].first
    assert_equal [1, 50, 7, 20, 3], row.values_at(:own_attack_records, :direct_damage, :flare_damage, :foreign_damage, :damage_taken)
    assert_equal({ 'cs_td' => [100, 100] }, row[:observed_defenses])
    assert_equal 3, row[:attack_refs].size
    assert_equal({ mana: 90 }, row[:final_resources])
    refute JSON.generate(result).include?(@path)
    assert_empty @observers.handlers
    assert_equal 3, @db.get_first_value('SELECT COUNT(*) FROM attacks')
  end

  def test_duplicate_receipts_do_not_double_count
    2.times { @observers.emit(receipt) }
    result = report
    assert_equal 1, result[:trials].first[:own_attack_records]
    assert_equal 'observed', result[:status]
  end

  def test_mixed_accepted_and_unattributable_receipts_mark_report_partial
    @observers.emit(receipt(1, source: nil))
    @observers.emit(receipt(2))
    result = report
    assert_equal 'partial', result[:status]
    assert_includes result[:limitations], 'some receipts were rejected; observations may be incomplete'
    assert_equal [{ session_id: 1, attack_id: 2 }], result[:trials].first[:attack_refs]
    assert_equal({ received: 2, accepted: 1, missing_source: 1 }, result[:receipt_counts])
  end

  def test_retaliation_is_own_damage_but_not_own_attack_or_cast
    @db.execute('INSERT INTO hits VALUES(3,1,1,9)')
    @observers.emit(receipt(3))
    row = report[:trials].first
    assert_equal [0, 9, 3], row.values_at(:own_attack_records, :flare_damage, :damage_taken)
  end

  def test_unowned_damage_does_not_become_our_attack
    @db.execute('UPDATE attacks SET unowned=1 WHERE id=1')
    @observers.emit(receipt)
    row = report[:trials].first
    assert_equal [0, 0, 0, 57], row.values_at(:own_attack_records, :direct_damage, :flare_damage, :unowned_damage)
  end

  def test_receipt_overflow_is_explicit
    # Extra events are outside the trial window and need no attack-row fixture.
    @observers.emit(receipt)
    (2..130).each { |id| @observers.emit(receipt(id, source: receipt(id)[:source].merge(received_at: 20))) }
    result = report
    assert_equal 'partial', result[:status]
    assert_includes result[:limitations], 'receipt capacity exceeded'
    assert_equal 1, result[:trials].first[:own_attack_records]
  end

  def test_large_trial_metadata_is_omitted_not_silently_truncated
    @observers.emit(receipt)
    result = report(trials: [trial(routine: 'x' * 4000)])
    assert_operator JSON.generate(result).length, :<=, LabCombatReport::MAX_JSON
    assert_equal 'partial', result[:status]
    assert_equal 1, result[:omitted_trials]
  end

  def test_rejects_other_connection_game_and_character
    [{ connection_id: 2 }, { game: 'GST' }, { character: 'Other' }].each do |change|
      @observers.emit(receipt(source: receipt[:source].merge(change)))
    end
    assert_equal 'no_attributable_recorder_receipts', report[:reason]
  end

  def test_missing_room_time_or_target_association_does_not_claim_zero_attacks
    @observers.emit(receipt)
    [trial(recording_context: @context.merge(room_epoch: 8)), trial(started_at: 20, finished_at: 21),
     trial(target_id: '999')].each do |item|
      row = report(trials: [item])[:trials].first
      assert_equal 'unavailable', row[:status]
      refute row.key?(:attempts)
    end
  end

  def test_unverified_return_never_reads_database
    @observers.emit(receipt)
    @db.execute('DROP TABLE attacks')
    assert_equal 'safe_handoff_unverified', report(safe: false)[:reason]
  end

  def test_multiple_recorders_fail_closed
    @observers.emit(receipt)
    @observers.emit(receipt(recorder_id: 'b' * 32))
    assert_equal 'multiple_recorders', report[:reason]
  end

  def test_replaced_database_is_not_read
    @observers.emit(receipt(file_identity: [0, 0]))
    assert_equal 'database_replaced', report[:reason]
  end

  def test_schema_mismatch_is_unavailable
    @observers.emit(receipt)
    @db.execute('DROP TABLE resolutions')
    assert_equal 'recorder_read_failed', report[:reason]
  end

  def test_absent_provenance_is_not_inferred
    @observers.emit(receipt(source: nil))
    assert_equal 'unavailable', report[:status]
  end

  def test_diagnostics_distinguish_no_callback_missing_source_and_binding_mismatch
    assert_equal({ received: 0, accepted: 0 }, report[:receipt_counts])
    @capture = capture
    @observers.emit(receipt(source: nil))
    @observers.emit(receipt(source: receipt[:source].merge(connection_id: 99)))
    result = report
    assert_equal 'no_attributable_recorder_receipts', result[:reason]
    assert_equal({ received: 2, accepted: 0, missing_source: 1, binding_mismatch: 1 }, result[:receipt_counts])
    assert_empty result[:trials]
  end

  def test_diagnostics_count_duplicates_without_leaking_receipt_contents
    @observers.emit(receipt)
    @observers.emit(receipt)
    @observers.emit(receipt(database: '/private/secret.db'))
    @observers.emit('private raw game text')
    result = report
    assert_equal({ received: 4, accepted: 1, duplicate: 1, database_mismatch: 1,
                   invalid_receipt: 1 }, result[:receipt_counts])
    refute_match(/private|secret|raw game text|#{Regexp.escape(@path)}/, JSON.generate(result))
    assert_equal 1, result[:trials].first[:own_attack_records]
  end

  def test_diagnostics_are_bounded_and_frozen_at_close
    10_005.times { @observers.emit(receipt(source: nil)) }
    result = report
    assert_equal 9999, result[:receipt_counts][:missing_source]
    assert_equal 9999, result[:receipt_counts][:received]
    @capture.receive(receipt)
    assert_equal result, report
    assert_operator JSON.generate(result).length, :<=, LabCombatReport::MAX_JSON
  end

  def test_closes_only_own_subscription_once_and_ignores_late_receipts
    other = @observers.on(:recorded_attack) { }
    @capture.close
    @capture.close
    @capture.receive(receipt)
    assert_equal [other], @observers.handlers
    assert_equal 1, @observers.removed.size
    assert_equal 'unavailable', report[:status]
  end

  def test_observer_cleanup_exception_does_not_escape
    @observers.define_singleton_method(:off) { |_| raise 'observer unavailable' }
    @capture.close
    assert_equal 'unavailable', report[:status]
  end

  def test_query_deadline_fails_soft
    ticks = 0
    @capture.close
    @capture = capture(clock: -> { ticks += 1; ticks * 0.2 })
    @observers.emit(receipt)
    assert_equal 'recorder_read_failed', report[:reason]
  end
end
