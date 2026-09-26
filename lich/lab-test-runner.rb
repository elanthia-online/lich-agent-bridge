# frozen_string_literal: true

require 'digest'
require 'json'
require 'securerandom'
require 'fileutils'

# Reviewed scripts still execute with full Lich privileges. This is a bounded
# test protocol, not a Ruby sandbox or a rollback mechanism.
module LabTestRunner
  class Invalid < StandardError; end
  class Halt < StandardError; end
  LOADED_PATH = File.realpath(__FILE__)
  LOADED_SHA256 = Digest::SHA256.file(__FILE__).hexdigest
  FIELDS = %w[room_id right_hand_id left_hand_id health mana spirit dead stunned].freeze
  ID = /\A[a-z][a-z0-9_-]{0,47}\z/
  SCRIPT = /\A[a-z][a-z0-9_-]{0,63}\z/
  TOKEN = /\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\z/
  DIGEST = /\A[0-9a-f]{64}\z/

  class UniqueObject < Hash
    def []=(key, value)
      raise Invalid, 'duplicate JSON field' if key?(key)
      super
    end
  end

  def self.strict(value, keys, required = keys)
    raise Invalid, 'invalid object fields' unless value.is_a?(Hash) &&
      (value.keys - keys).empty? && (required - value.keys).empty?
  end

  def self.path(value)
    raise Invalid, 'invalid relative path' unless value.is_a?(String) && value.length <= 240 &&
      value.split('/', -1).all? { |part| part.match?(/\A[A-Za-z0-9_-][A-Za-z0-9_.-]*\z/) && !%w[. ..].include?(part) }
    value
  end

  def self.metadata!(value)
    strict(value, %w[manifest files])
    path(value['manifest'])
    raise Invalid, 'manifest must be JSON' unless value['manifest'].end_with?('.json')
    files = value['files']
    raise Invalid, 'invalid pinned files' unless files.is_a?(Hash) && files.length.between?(4, 67) &&
      files.keys.map(&:downcase).uniq.length == files.length
    files.each do |name, digest|
      path(name)
      raise Invalid, 'invalid file digest' unless digest.is_a?(String) && DIGEST.match?(digest)
    end
    raise Invalid, 'missing runner or manifest pin' unless
      [value['manifest'], 'lab-test-runner.rb', 'lab-test-runner.lic'].all? { |name| files.key?(name) }
    value
  end

  class Suite
    attr_reader :data, :root, :metadata, :cases, :revision

    def initialize(root:, metadata:, suite_id:, revision:, case_id:, resolve:)
      @root = File.realpath(root)
      @metadata = LabTestRunner.metadata!(metadata)
      @revision, @resolve = revision, resolve
      raise Invalid, 'revision does not match manifest pin' unless revision == metadata['files'][metadata['manifest']]
      verify_files!
      manifest = absolute(metadata['manifest'])
      raise Invalid, 'suite manifest too large' if File.size(manifest) > 65_536
      @data = JSON.parse(File.read(manifest, encoding: 'UTF-8'), object_class: UniqueObject)
      validate!
      raise Invalid, 'suite identity mismatch' unless data['id'] == suite_id
      @cases = case_id == 'all' ? data['cases'] : data['cases'].select { |item| item['id'] == case_id }
      raise Invalid, 'unregistered case' if @cases.empty?
      verify!
    rescue SystemCallError, JSON::ParserError => error
      raise Invalid, "unavailable suite: #{error.class}"
    end

    def absolute(relative)
      name = LabTestRunner.path(relative)
      path = File.realpath(File.join(root, name))
      raise Invalid, 'file escapes scripts directory' unless path.start_with?(root + File::SEPARATOR)
      raise Invalid, 'symlink substitution is not allowed' unless path == File.join(root, name)
      raise Invalid, 'pinned path is not a regular file' unless File.file?(path)
      path
    end

    def verify_files!
      metadata['files'].each do |relative, digest|
        raise Invalid, 'pinned file changed' unless Digest::SHA256.file(absolute(relative)).hexdigest == digest
      end
      raise Invalid, 'loaded runner code differs from pin' unless metadata['files']['lab-test-runner.rb'] == LOADED_SHA256
    end

    def verify!
      verify_files!
      required = [metadata['manifest'], 'lab-test-runner.lic', 'lab-test-runner.rb', *data['files']].uniq
      raise Invalid, 'pins do not match declared files' unless required.sort == metadata['files'].keys.sort
      target = data['files'].select { |path| File.basename(path) == "#{data['script']}.lic" }
      raise Invalid, 'exact target must be declared once' unless target.length == 1
      verify_resolution!(data['script'], target.first)
      verify_resolution!('lab-test-runner', 'lab-test-runner.lic')
      true
    rescue SystemCallError => error
      raise Invalid, "pinned file unavailable: #{error.class}"
    end

    def verify_resolution!(name, expected)
      dirs = [root, File.join(root, 'custom')]
      dirs.concat(Dir.glob(File.join(root, 'custom', '*')).select { |dir| File.directory?(dir) })
      raise Invalid, 'symlink script directory' if dirs.any? { |dir| File.symlink?(dir) }
      matches = dirs.select { |dir| File.directory?(dir) }.flat_map do |dir|
        Dir.children(dir).select { |file| file.match?(/\A#{Regexp.escape(name)}\.(?:lic|rb|cmd|wiz)(?:\.(?:gz|Z))?\z/i) }
          .map { |file| File.join(dir, file) }
      end
      # The fixed wrapper and its pinned Ruby helper deliberately share a stem.
      matches.delete(File.join(root, 'lab-test-runner.rb')) if name == 'lab-test-runner'
      raise Invalid, 'ambiguous script resolution' unless matches == [File.join(root, expected)]
      resolved = @resolve.call(name).to_s.sub(%r{\A/}, '')
      raise Invalid, 'script resolver selected unapproved file' unless resolved == expected
    end

    def validate!
      LabTestRunner.strict(data, %w[version id script files cases limits])
      raise Invalid, 'invalid suite identity' unless data['version'].instance_of?(Integer) && data['version'] == 1 && data['id'].is_a?(String) && ID.match?(data['id']) &&
        data['script'].is_a?(String) && SCRIPT.match?(data['script']) && data['script'] != 'lab-test-runner'
      files = data['files']
      raise Invalid, 'invalid declared files' unless files.is_a?(Array) && files.length.between?(1, 64) &&
        files.all? { |name| name.is_a?(String) } && files.map(&:downcase).uniq.length == files.length
      files.each { |name| LabTestRunner.path(name) }
      limits = data['limits']
      LabTestRunner.strict(limits, %w[case_seconds run_seconds cleanup_seconds])
      raise Invalid, 'invalid time limits' unless limits.values.all? { |n| n.is_a?(Numeric) && n.finite? && n.positive? } &&
        limits['case_seconds'] <= limits['run_seconds'] && limits['run_seconds'] <= 20 && limits['cleanup_seconds'] <= 3
      cases = data['cases']
      raise Invalid, 'invalid case count' unless cases.is_a?(Array) && cases.length.between?(1, 20)
      cases.each do |item|
        LabTestRunner.strict(item, %w[id args assertions])
        raise Invalid, 'invalid case identity' unless item['id'].is_a?(String) && ID.match?(item['id']) && item['id'] != 'all'
        args = item['args']
        raise Invalid, 'invalid fixed arguments' unless args.is_a?(Array) && args.length <= 32 && args.all? { |arg| arg.is_a?(String) && TOKEN.match?(arg) }
        assertions = item['assertions']
        raise Invalid, 'assertions required' unless assertions.is_a?(Array) && assertions.length.between?(1, 32)
        assertions.each do |assertion|
          LabTestRunner.strict(assertion, %w[field op value], %w[field op])
          raise Invalid, 'unsupported assertion' unless FIELDS.include?(assertion['field']) && %w[equals unchanged].include?(assertion['op'])
          if assertion['op'] == 'equals'
            value = assertion['value']
            raise Invalid, 'invalid assertion value' unless value == true || value == false ||
              (value.is_a?(Numeric) && value.finite?) || (value.is_a?(String) && value.length <= 256)
          else
            raise Invalid, 'unchanged assertion has value' if assertion.key?('value')
          end
        end
      end
      raise Invalid, 'duplicate case' unless cases.map { |item| item['id'] }.uniq.length == cases.length
    end
  end

  class Runner
    attr_reader :children

    def initialize(suite:, context:, snapshot:, control:, report_directory:, start_child:)
      @suite, @context, @snapshot, @control = suite, context, snapshot, control
      @report_directory, @start_child = report_directory, start_child
      @children = []
      @control_mutex = Mutex.new
    end

    def now = Process.clock_gettime(Process::CLOCK_MONOTONIC)

    def cleanup_complete?
      children.all? { |child| child.join(0) }
    end

    def observe!
      state = @snapshot.call
      raise Halt, 'local state is unavailable' unless state.is_a?(Hash)
      raise Halt, 'identity or room changed' unless %w[character generation room_id].all? { |key| state[key] == @context[key] }
      raise Halt, 'alive and unstunned state required' unless state['dead'] == false && state['stunned'] == false
      state
    end

    def check!
      state = observe!
      valid = @control_mutex.synchronize { @control_ok && now - @control_at <= 2.5 }
      raise Halt, 'run control revoked or unavailable' unless valid
      state
    end

    def poll_control
      valid = @control.call == true
      @control_mutex.synchronize { @control_ok, @control_at = valid, now }
      valid
    rescue StandardError
      @control_mutex.synchronize { @control_ok, @control_at = false, now }
      false
    end

    def run
      @suite.verify!
      prepare_report
      report = {'suite_id' => @context['suite_id'], 'revision' => @context['revision'],
                'files' => @suite.metadata.fetch('files').dup,
                'case_id' => @context['case_id'], 'action_id' => @context['action_id'],
                'character' => @context['character'], 'generation' => @context['generation'], 'cases' => []}
      remaining = @context['expires_at'] && @context['expires_at'] - Time.now.to_f - @suite.data['limits']['cleanup_seconds']
      work_seconds = [@suite.data['limits']['run_seconds'], remaining].compact.min
      deadline = now + work_seconds
      poll_control
      @poller = Thread.new do
        loop do
          sleep 1
          break unless poll_control
        end
      end
      stopped = false
      @suite.cases.each do |item|
        if stopped
          report['cases'] << {'id' => item['id'], 'status' => 'skipped', 'reason' => 'prior case did not pass'}
          next
        end
        record = run_case(item, deadline)
        report['cases'] << record
        stopped = record['status'] != 'passed'
      end
      clean = cleanup_complete?
      passed = clean && report['cases'].all? { |item| item['status'] == 'passed' }
      status = passed ? 'passed' : (report['cases'].any? { |item| item['status'] == 'failed' } ? 'failed' : 'inconclusive')
      report.merge!('status' => status, 'cleanup_complete' => clean)
      path = write_report(report)
      {ok: passed, code: clean ? status : 'cleanup_incomplete', message: "Script test #{status}; cleanup #{clean ? 'complete' : 'incomplete'}.",
       details: {suite_id: @context['suite_id'], revision: @context['revision'], case_id: @context['case_id'],
                 status: status, assertions_passed: passed, cleanup_complete: clean, report_path: path}}
    ensure
      @poller&.kill
      @poller&.join(0.1)
      @report_file&.close
    end

    def run_case(item, deadline)
      record = {'id' => item['id'], 'args' => item['args'], 'status' => 'inconclusive', 'assertions' => []}
      child = nil
      begin
        check!
        raise Halt, 'run deadline reached' if now >= deadline
        @suite.verify!
        # Pin verification can consume time or overlap a local state change.
        # Revalidate immediately before starting any child.
        before = check!
        raise Halt, 'run deadline reached' if now >= deadline
        record['before'] = before
        case_deadline = [deadline, now + @suite.data['limits']['case_seconds']].min
        child = @start_child.call(@suite.data['script'], item['args'].join(' '))
        raise Halt, 'child startup rejected' unless child
        @children << child
        raise Halt, 'unsupported child lifecycle' unless %i[join kill_sync completed_successfully? exit_error].all? { |method| child.respond_to?(method) }
        loop do
          check!
          completed = child.join(0)
          raise Halt, 'case deadline reached' if now >= case_deadline
          break if completed
          sleep [0.05, case_deadline - now].min.clamp(0, 0.05)
        end
        after = check!
        record['after'] = after
        error = child.exit_error
        record['lifecycle'] = {'completed_successfully' => child.completed_successfully?,
                               'error' => error && {'class' => error.class.name, 'message' => error.message.to_s[0, 500]}}
        record['assertions'] = item['assertions'].map do |assertion|
          field = assertion['field']
          expected = assertion['op'] == 'unchanged' ? before[field] : assertion['value']
          actual = after[field]
          known = !actual.nil? && !expected.nil?
          assertion.merge('expected' => expected, 'actual' => actual, 'passed' => known && actual == expected, 'known' => known)
        end
        record['status'] = if !child.completed_successfully? || record['assertions'].any? { |a| a['known'] && !a['passed'] }
                             'failed'
                           elsif record['assertions'].all? { |a| a['passed'] }
                             'passed'
                           else
                             'inconclusive'
                           end
      rescue StandardError => error
        record['reason'] = "#{error.class}: #{error.message}"[0, 500]
      ensure
        if child && child.respond_to?(:join) && !child.join(0)
          begin
            cleanup_seconds = @suite.data['limits']['cleanup_seconds']
            cleanup_seconds = [cleanup_seconds, [@context['expires_at'] - Time.now.to_f, 0].max].min if @context['expires_at']
            child.kill_sync(timeout: cleanup_seconds)
          rescue StandardError => error
            record['cleanup_error'] = error.class.name
          end
        end
        record['cleanup_complete'] = !child || (child.respond_to?(:join) && !!child.join(0))
        begin
          record['after'] ||= observe!
        rescue StandardError => error
          record['restoration_error'] = error.class.name
        end
      end
      record
    end

    def prepare_report
      if File.exist?(@report_directory)
        stat = File.lstat(@report_directory)
        raise Invalid, 'unsafe report directory' unless stat.directory? && !stat.symlink? && stat.uid == Process.uid && (stat.mode & 0o077).zero?
      else
        Dir.mkdir(@report_directory, 0o700)
      end
      @report_path = File.join(@report_directory, "run-#{SecureRandom.hex(16)}.json")
      @report_file = File.open(@report_path, File::WRONLY | File::CREAT | File::EXCL, 0o600)
    rescue SystemCallError => error
      raise Invalid, "private report is unavailable: #{error.class}"
    end

    def write_report(report)
      @report_file.write(JSON.generate(report))
      @report_file.flush
      @report_path
    end
  end
end
