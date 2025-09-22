using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEditor.TestTools.TestRunner.Api;
using UnityEngine;

namespace MCPForUnity.Editor
{
    /// <summary>
    /// Helper entry points invoked by the MCP automation layer. These methods are intentionally
    /// lightweight so they can be triggered either from the running editor instance or via
    /// Unity's batchmode invocation using -executeMethod.
    /// </summary>
    public static class AIBuild
    {
        /// <summary>
        /// Write a UTF-8 JSON payload to the provided path, creating parent directories if
        /// necessary. Returns the absolute path that was written.
        /// </summary>
        public static string WriteJson(string path, string json)
        {
            if (string.IsNullOrEmpty(path))
            {
                throw new ArgumentException("Path cannot be null or empty", nameof(path));
            }

            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
            {
                Directory.CreateDirectory(directory);
            }

            File.WriteAllText(path, json ?? "{}", new UTF8Encoding(false));
            return Path.GetFullPath(path);
        }

        /// <summary>
        /// Trigger a domain refresh/compilation and wait until the editor is done compiling.
        /// </summary>
        public static void VerifyCompile()
        {
            AssetDatabase.Refresh();
            var start = DateTime.UtcNow;
            while (CompilationPipeline.isCompiling)
            {
                Thread.Sleep(100);
                if ((DateTime.UtcNow - start).TotalMinutes > 10)
                {
                    throw new TimeoutException("Compilation did not complete within 10 minutes.");
                }
            }
        }

        /// <summary>
        /// Execute edit mode or play mode tests and ensure an NUnit compatible XML file is written.
        /// The method blocks until test execution completes.
        /// </summary>
        public static string RunTests(string testMode, string resultsPath = "Logs/results.xml")
        {
            if (string.IsNullOrWhiteSpace(testMode))
            {
                throw new ArgumentException("testMode is required", nameof(testMode));
            }

            string resolvedPath = ResolveResultsPath(resultsPath);
            EnsureDirectory(resolvedPath);

            var api = ScriptableObject.CreateInstance<TestRunnerApi>();
            try
            {
                var filter = new Filter
                {
                    testMode = string.Equals(testMode, "PlayMode", StringComparison.OrdinalIgnoreCase)
                        ? TestMode.PlayMode
                        : TestMode.EditMode
                };

                using var completion = new ManualResetEvent(false);
                ITestResultAdaptor finalResult = null;
                api.RegisterCallbacks(new TestCallbacks(resolvedPath, r =>
                {
                    finalResult = r;
                    completion.Set();
                }));

                var settings = new ExecutionSettings
                {
                    filters = new[] { filter },
                    runSynchronously = true
                };

                api.Execute(settings);

                if (!completion.WaitOne(TimeSpan.FromMinutes(30)))
                {
                    throw new TimeoutException("Test execution timed out after 30 minutes.");
                }

                if (finalResult == null)
                {
                    throw new InvalidOperationException("Test execution did not produce a result.");
                }

                // The callback already wrote the XML; return the resolved path for convenience.
                return resolvedPath;
            }
            finally
            {
                ScriptableObject.DestroyImmediate(api);
            }
        }

        private static string ResolveResultsPath(string path)
        {
            if (Path.IsPathRooted(path))
            {
                return Path.GetFullPath(path);
            }

            string projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            return Path.GetFullPath(Path.Combine(projectRoot, path));
        }

        private static void EnsureDirectory(string filePath)
        {
            string directory = Path.GetDirectoryName(filePath);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
            {
                Directory.CreateDirectory(directory);
            }
        }

        private sealed class TestCallbacks : ICallbacks
        {
            private readonly string _resultsPath;
            private readonly Action<ITestResultAdaptor> _onComplete;

            public TestCallbacks(string resultsPath, Action<ITestResultAdaptor> onComplete)
            {
                _resultsPath = resultsPath;
                _onComplete = onComplete;
            }

            public void RunStarted(ITestAdaptor testsToRun)
            {
            }

            public void RunFinished(ITestResultAdaptor result)
            {
                try
                {
                    WriteNUnitXml(_resultsPath, result);
                }
                catch (Exception ex)
                {
                    Debug.LogError($"[AIBuild] Failed to write test results XML: {ex.Message}\n{ex.StackTrace}");
                }
                finally
                {
                    _onComplete?.Invoke(result);
                }
            }

            public void TestStarted(ITestAdaptor test)
            {
            }

            public void TestFinished(ITestResultAdaptor result)
            {
            }

            private static void WriteNUnitXml(string path, ITestResultAdaptor result)
            {
                var summary = new NUnitSummary(result);
                var builder = new System.Xml.Linq.XDocument(
                    new System.Xml.Linq.XDeclaration("1.0", "utf-8", "yes"),
                    new System.Xml.Linq.XElement(
                        "test-run",
                        new System.Xml.Linq.XAttribute("id", "0"),
                        new System.Xml.Linq.XAttribute("name", Application.productName ?? "Unity Project"),
                        new System.Xml.Linq.XAttribute("result", summary.Result),
                        new System.Xml.Linq.XAttribute("total", summary.Total),
                        new System.Xml.Linq.XAttribute("passed", summary.Passed),
                        new System.Xml.Linq.XAttribute("failed", summary.Failed),
                        new System.Xml.Linq.XAttribute("skipped", summary.Skipped),
                        new System.Xml.Linq.XAttribute("duration", summary.DurationSeconds.ToString("F3")),
                        new System.Xml.Linq.XElement(
                            "test-suite",
                            new System.Xml.Linq.XAttribute("type", "Assembly"),
                            new System.Xml.Linq.XAttribute("name", result.Name ?? "Tests"),
                            new System.Xml.Linq.XAttribute("result", summary.Result),
                            BuildTestCaseElements(result)
                        )
                    )
                );

                using var stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None);
                builder.Save(stream);
            }

            private static IEnumerable<System.Xml.Linq.XElement> BuildTestCaseElements(ITestResultAdaptor root)
            {
                foreach (var child in root.Children ?? Array.Empty<ITestResultAdaptor>())
                {
                    if (child.HasChildren)
                    {
                        yield return new System.Xml.Linq.XElement(
                            "test-suite",
                            new System.Xml.Linq.XAttribute("type", child.Test?.HasChildren == true ? "Suite" : "Test"),
                            new System.Xml.Linq.XAttribute("name", child.Name ?? "Unnamed"),
                            new System.Xml.Linq.XAttribute("result", child.ResultState?.Status.ToString() ?? "Unknown"),
                            BuildTestCaseElements(child)
                        );
                    }
                    else
                    {
                        yield return new System.Xml.Linq.XElement(
                            "test-case",
                            new System.Xml.Linq.XAttribute("name", child.Name ?? "Unnamed"),
                            new System.Xml.Linq.XAttribute("fullname", child.FullName ?? child.Name ?? "Unnamed"),
                            new System.Xml.Linq.XAttribute("result", child.ResultState?.Status.ToString() ?? "Unknown"),
                            new System.Xml.Linq.XAttribute("duration", child.Duration.ToString("F3")),
                            child.ResultState?.Status == TestStatus.Failed && !string.IsNullOrEmpty(child.Message)
                                ? new System.Xml.Linq.XElement(
                                    "failure",
                                    new System.Xml.Linq.XElement("message", child.Message ?? string.Empty),
                                    new System.Xml.Linq.XElement("stack-trace", child.StackTrace ?? string.Empty)
                                )
                                : null
                        );
                    }
                }
            }
        }

        private sealed class NUnitSummary
        {
            public NUnitSummary(ITestResultAdaptor result)
            {
                Total = result?.TestCaseCount ?? 0;
                Passed = result?.PassCount ?? 0;
                Failed = result?.FailCount ?? 0;
                Skipped = result?.SkipCount ?? 0;
                DurationSeconds = result?.Duration ?? 0.0;
                Result = result?.ResultState?.Status.ToString() ?? "Unknown";
            }

            public int Total { get; }
            public int Passed { get; }
            public int Failed { get; }
            public int Skipped { get; }
            public double DurationSeconds { get; }
            public string Result { get; }
        }
    }
}
