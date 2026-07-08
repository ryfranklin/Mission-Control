// Mission Control eval gate — Jenkins pipeline (mirrors the Liquibase pattern).
//
//   NONPROD : run the G3 gate CLI automatically; a nonzero exit (a quality OR
//             total-cost regression) FAILS the build and blocks promotion.
//             The JSON result + human report are archived as build artifacts.
//   PROMOTE : a manual go/no-go input step — this IS the Mission Control
//             go/no-go gate. It is only reachable if NONPROD passed, because a
//             failed stage aborts the pipeline before this stage runs.
//
// Runs in --demo mode (deterministic StubWorker, no live LLM) so the pipeline is
// demonstrable without live infra. Flip DEMO_SCENARIO to see red vs green.
// For a real gate, drop --demo and let it call the worker + Opus judge.

pipeline {
  agent any

  parameters {
    choice(
      name: 'DEMO_SCENARIO',
      choices: ['clean', 'regression'],
      description: 'clean = passes → reaches the manual gate; regression = reddens NONPROD, promotion never offered'
    )
    // Real gate knobs (env-equivalent: MC_GATE_K / MC_GATE_N).
    string(name: 'K', defaultValue: '2', description: 'noise-band width in stddevs')
    string(name: 'N', defaultValue: '1', description: 'eval repeats to average')
  }

  options { timestamps() }

  environment {
    BASELINE = "ci/demo/baseline.${params.DEMO_SCENARIO == 'regression' ? 'regressed' : 'pass'}.json"
  }

  stages {
    stage('Prepare') {
      steps {
        // Package must be importable on the agent (pip install -e . once).
        sh 'python ci/demo/setup.py'
      }
    }

    stage('NONPROD — G3 eval gate') {
      steps {
        sh '''#!/bin/bash
          set -o pipefail
          mkdir -p artifacts artifacts/evals
          python -m mission_control.eval_gate --demo \
            --tasks ci/demo/tasks --sandbox ci/demo/sandbox \
            --baseline "$BASELINE" --k "$K" --n "$N" \
            --json artifacts/gate-result.json --out-dir artifacts/evals \
            | tee artifacts/gate-report.txt
        '''
        // pipefail => a nonzero gate exit fails this stage, which aborts the
        // pipeline and blocks PROMOTE. Regression => promotion never offered.
      }
      post {
        always {
          archiveArtifacts artifacts: 'artifacts/gate-result.json, artifacts/gate-report.txt',
                           fingerprint: true, onlyIfSuccessful: false
        }
      }
    }

    stage('PROMOTE — prod go/no-go') {
      // Reached ONLY when NONPROD passed (a failed stage aborts the build first).
      steps {
        script {
          // The Mission Control go/no-go gate, literally: a manual approval.
          def decision = input(
            id: 'go-no-go',
            message: 'Mission Control go/no-go — promote to prod?',
            ok: 'Decide',
            parameters: [choice(name: 'GATE', choices: ['go', 'no-go'], description: 'go = promote; no-go = stop')]
          )
          if (decision == 'no-go') {
            error('Promotion rejected at the go/no-go gate (no-go).')
          }
          echo 'go — promoting to prod.'
          // ... real prod deploy step goes here ...
        }
      }
    }
  }

  post {
    failure { echo 'Pipeline RED — regression or no-go. Promotion did not happen.' }
    success { echo 'Pipeline GREEN — promoted.' }
  }
}
