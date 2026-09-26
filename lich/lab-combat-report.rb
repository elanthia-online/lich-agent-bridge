# frozen_string_literal: true

# Read committed core Recorder rows; never start a recorder or send commands.
require 'json'
require 'thread'

module LabCombatReport
  MAX_RECEIPTS = 128
  MAX_JSON = 3500
  MAX_DIAGNOSTIC_COUNT = 9999
  CLOCK = -> { Process.clock_gettime(Process::CLOCK_MONOTONIC) }
  BINDING = %i[connection_id game character].freeze

  def self.unavailable(reason)
    { version: 1, status: 'unavailable', reason: reason, trials: [] }
  end

  class Capture
    def initialize(character:, context:, database:, observers:, clock: CLOCK)
      @character, @context, @database = character, context, File.expand_path(database)
      @database = File.realpath(@database) if File.file?(@database)
      @observers, @clock = observers, clock
      @mutex, @receipts = Mutex.new, []
      @receipt_counts = { received: 0, accepted: 0 }
      @overflow, @closed = false, false
      @handler = observers.on(:recorded_attack) { |_type, receipt| receive(receipt) }
    end

    def close
      handler = @mutex.synchronize do
        @closed = true
        previous, @handler = @handler, nil
        previous
      end
      @observers.off(handler) if handler
    rescue StandardError
      nil # an observer failure must not interrupt controller cleanup
    end

    def receive(receipt)
      reason = rejection_reason(receipt)
      unless reason
        copy = receipt.slice(:protocol, :recorder_id, :database, :file_identity, :session_id, :attack_id)
        copy[:source] = receipt[:source].slice(*BINDING, :room_epoch, :sequence, :received_at)
        copy = JSON.parse(JSON.generate(copy), symbolize_names: true)
      end
      retain_receipt(copy, reason)
    rescue StandardError
      begin
        retain_receipt(nil, :callback_error)
      rescue StandardError
        nil # reporting must never raise into core's combat observer
      end
    end

    def report(trials:, safe:)
      close
      return unavailable('safe_handoff_unverified') unless safe == true
      receipts, overflow = @mutex.synchronize { [@receipts.dup, @overflow] }
      return unavailable('no_attributable_recorder_receipts') if receipts.empty?
      return unavailable('multiple_recorders') unless receipts.map { |r| r[:recorder_id] }.uniq.length == 1
      return unavailable('database_replaced') unless receipts.map { |r| r[:file_identity] }.uniq.length == 1
      return unavailable('missing_trial_context') unless trials.is_a?(Array) && trials.length.between?(1, 5)
      require 'sqlite3'
      identity = receipts.first[:file_identity]
      return unavailable('database_replaced') unless file_identity == identity
      deadline = @clock.call + 0.1
      db = SQLite3::Database.new(@database, readonly: true)
      db.busy_timeout = 25
      return unavailable('query_timeout_unsupported') unless db.respond_to?(:statement_timeout=)
      db.statement_timeout = 25
      db.execute('BEGIN')
      reports = trials.map do |trial|
        raise 'report deadline' if @clock.call >= deadline
        trial_report(db, trial, receipts, deadline)
      end
      return unavailable('database_replaced') unless file_identity == identity
      counts = receipt_counts
      rejected = counts.any? { |key, value| !%i[received accepted duplicate].include?(key) && value.positive? }
      result = {
        version: 1, status: overflow || rejected || reports.any? { |r| r[:status] != 'observed' } ? 'partial' : 'observed',
        recorder_id: receipts.first[:recorder_id], trials: reports, receipt_counts: counts,
        limitations: ['observed association, not causal proof',
                       'missing or delayed events may be absent',
                       'resource values are net observations, not spell costs']
      }
      result[:limitations] << 'some receipts were rejected; observations may be incomplete' if rejected
      result[:limitations] << 'receipt capacity exceeded' if overflow
      while JSON.generate(result).length > MAX_JSON && !result[:trials].empty?
        result[:trials].pop
        result[:status] = 'partial'
        result[:omitted_trials] = result.fetch(:omitted_trials, 0) + 1
      end
      result
    rescue StandardError, LoadError
      unavailable('recorder_read_failed')
    ensure
      begin
        db&.close
      rescue StandardError
        nil
      end
    end

    private

    # Fixed reason names and saturating counters only: never retain rejected
    # identities, database paths, raw game text, or one record per rejection.
    def rejection_reason(receipt)
      return :invalid_receipt unless receipt.is_a?(Hash) && receipt[:protocol] == 1
      source = receipt[:source]
      return :missing_source unless source.is_a?(Hash)
      return :missing_context unless @context.is_a?(Hash)
      return :binding_mismatch unless BINDING.all? { |key| source[key] == @context[key] }
      return :character_mismatch unless source[:character].to_s.casecmp?(@character)
      return :invalid_source unless %i[connection_id sequence].all? { |key| source[key].is_a?(Integer) && source[key].positive? } &&
                                    source[:room_epoch].is_a?(Integer) && source[:room_epoch] >= 0 &&
                                    source[:received_at].is_a?(Numeric) && source[:received_at].finite? && source[:received_at] >= 0
      return :invalid_record_ids unless %i[attack_id session_id].all? { |key| receipt[key].is_a?(Integer) && receipt[key].positive? }
      return :invalid_recorder_id unless receipt[:recorder_id].is_a?(String) && receipt[:recorder_id].match?(/\A[0-9a-f]{32}\z/)
      return :database_mismatch unless receipt[:database] == @database
      return :invalid_file_identity unless receipt[:file_identity].is_a?(Array) &&
                                           receipt[:file_identity].length == 2 && receipt[:file_identity].all? { |v| v.is_a?(Integer) }
      nil
    end

    def retain_receipt(copy, reason)
      @mutex.synchronize do
        return if @closed
        unless reason
          if @receipts.any? { |r| r[:recorder_id] == copy[:recorder_id] && r[:attack_id] == copy[:attack_id] }
            reason = :duplicate
          elsif @receipts.length >= MAX_RECEIPTS
            @overflow = true
            reason = :capacity_exceeded
          else
            @receipts << copy
            reason = :accepted
          end
        end
        [:received, reason].each do |key|
          @receipt_counts[key] = [@receipt_counts.fetch(key, 0) + 1, MAX_DIAGNOSTIC_COUNT].min
        end
      end
    end

    def receipt_counts
      @mutex.synchronize { @receipt_counts.dup }
    end

    def unavailable(reason)
      LabCombatReport.unavailable(reason).merge(receipt_counts: receipt_counts)
    end

    def file_identity
      stat = File.stat(@database)
      [stat.dev, stat.ino]
    end

    def trial_report(db, trial, receipts, deadline)
      raise 'invalid trial' unless trial.is_a?(Hash)
      context, first, last = trial.values_at(:recording_context, :started_at, :finished_at)
      base = trial.slice(:index, :routine, :target_id, :outcome, :elapsed_seconds)
      return base.merge(status: 'unavailable', reason: 'missing_trial_context') unless
        context.is_a?(Hash) && BINDING.all? { |key| context[key] == @context[key] } &&
        context[:room_epoch].is_a?(Integer) && context[:room_epoch] >= 0 &&
        first.is_a?(Numeric) && last.is_a?(Numeric) && first.finite? && last.finite? && last >= first &&
        trial[:target_id].to_s.match?(/\A[0-9]+\z/)
      selected = receipts.select do |r|
        r[:source][:room_epoch] == context[:room_epoch] &&
          r[:source][:received_at] >= first && r[:source][:received_at] <= last
      end
      attempts = direct = flares = foreign = unowned = taken = 0
      refs = []
      defenses = {}
      selected.each do |r|
        raise 'report deadline' if @clock.call >= deadline
        row = db.get_first_row(<<~SQL, [r[:attack_id], r[:session_id]])
          SELECT a.inbound,a.foreign_caster,a.unowned,a.target_kind,c.exist_id,
                 a.attacker_exist_id,s.character
          FROM attacks a JOIN sessions s ON s.id=a.session_id
          LEFT JOIN creatures c ON c.id=a.creature_id
          WHERE a.id=? AND a.session_id=?
        SQL
        raise 'receipt row mismatch' unless row && row[6].to_s.casecmp?(@character)
        hit = db.get_first_row(<<~SQL, [r[:attack_id], trial[:target_id].to_i])
          SELECT COUNT(*),COALESCE(SUM(CASE WHEN h.flare_id IS NULL THEN h.damage ELSE 0 END),0),
                 COALESCE(SUM(CASE WHEN h.flare_id IS NOT NULL THEN h.damage ELSE 0 END),0)
          FROM hits h JOIN creatures c ON c.id=h.creature_id
          WHERE h.attack_id=? AND c.exist_id=?
        SQL
        incoming = row[0] == 1 && row[5].to_s == trial[:target_id].to_s
        targeted = row[4].to_s == trial[:target_id].to_s
        next unless targeted || incoming || hit[0].positive?
        refs << { session_id: r[:session_id], attack_id: r[:attack_id] }
        if row[1] == 1
          foreign += hit[1] + hit[2]
        elsif row[2] == 1
          unowned += hit[1] + hit[2]
        else
          # Match Recorder reporting ownership: inbound attacks may contain
          # our retaliatory flare hits on the creature, without being our cast.
          attempts += 1 if row[0] == 0 && targeted
          direct += hit[1]
          flares += hit[2]
        end
        if incoming
          taken += db.get_first_value('SELECT COALESCE(SUM(damage),0) FROM hits WHERE attack_id=? AND creature_id IS NULL', [r[:attack_id]])
        end
        next unless targeted && row[0] == 0
        db.execute('SELECT type,MIN(defender_stat),MAX(defender_stat) FROM resolutions WHERE attack_id=? AND flare_id IS NULL AND type IN (\'as_ds\',\'cs_td\',\'uaf_udf\') AND defender_stat IS NOT NULL GROUP BY type', [r[:attack_id]]).each do |type, low, high|
          prev = defenses[type]
          defenses[type] = prev ? [[prev[0], low].min, [prev[1], high].max] : [low, high]
        end
      end
      return base.merge(status: 'unavailable', reason: 'no_matching_attack_rows') if refs.empty?

      base.merge(status: 'observed',
                 own_attack_records: attempts, direct_damage: direct, flare_damage: flares,
                 foreign_damage: foreign, unowned_damage: unowned, damage_taken: taken,
                 observed_defenses: defenses, attack_refs: refs.first(8),
                 omitted_attack_refs: [refs.length - 8, 0].max,
                 final_resources: trial[:final_resources])
    end
  end
end
