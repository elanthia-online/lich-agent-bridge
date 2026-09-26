# frozen_string_literal: true

require 'minitest/autorun'
require 'tmpdir'
require 'open3'
require 'rbconfig'

class LabDependencySwitchTest < Minitest::Test
  def test_registry_follows_an_installed_symlink_when_switching_builds
    source = File.read(File.expand_path('../lich/lab-bridge.lic', __dir__))
    registry_require = source.lines.find { |line| line.start_with?('require ') && line.include?('controller_registry_core') }
    probe = <<~RUBY
      require 'tmpdir'
      Dir.mktmpdir('lab-registry-switch') do |root|
        first = File.join(root, 'first.rb')
        second = File.join(root, 'second.rb')
        link = File.join(root, 'registry.rb')
        File.write(first, '$registry_version = 1')
        File.write(second, '$registry_version = 2')
        File.symlink(first, link)
        controller_registry_core = link.delete_suffix('.rb')
        #{registry_require}
        raise 'first registry did not load' unless $registry_version == 1
        File.unlink(link)
        File.symlink(second, link)
        #{registry_require}
        raise 'old registry remained cached after build switch' unless $registry_version == 2
      end
    RUBY
    output, error, status = Open3.capture3(RbConfig.ruby, '-e', probe)
    assert status.success?, "#{output}\n#{error}"
  end
end
