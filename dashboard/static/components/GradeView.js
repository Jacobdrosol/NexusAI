/**
 * GradeView - AI Grading Results Display Component
 * Handles display of AI grading results, feedback, and premium-gated features
 */
class GradeView {
  constructor(options = {}) {
    this.container = options.container || null;
    this.isPremium = options.isPremium || false;
    this.onExpandRequested = options.onExpandRequested || (() => {});
    this.onExportRequested = options.onExportRequested || (() => {});
    this.onPremiumRequired = options.onPremiumRequired || (() => {});
    this.apiEndpoint = options.apiEndpoint || '/api/grading';
    this.csrfToken = options.csrfToken || '';
    this.gradingData = null;
    this.expandedSections = new Set();
  }

  /**
   * Set grading data and render
   * @param {object} data - Grading result data
   */
  setGradingData(data) {
    this.gradingData = data;
    if (this.container) {
      this.render();
    }
  }

  /**
   * Load grading data from API
   * @param {string} submissionId - Submission ID to load
   * @returns {Promise<object>} - Grading result
   */
  async loadGradingData(submissionId) {
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (this.csrfToken) {
        headers['X-CSRFToken'] = this.csrfToken;
      }

      const response = await fetch(`${this.apiEndpoint}/${encodeURIComponent(submissionId)}`, {
        method: 'GET',
        headers
      });

      if (!response.ok) {
        throw new Error(`Failed to load grading data: ${response.status}`);
      }

      const data = await response.json();
      this.setGradingData(data);
      return data;
    } catch (err) {
      console.error('GradeView: Failed to load grading data', err);
      this.renderError(err.message);
      throw err;
    }
  }

  /**
   * Calculate grade display value
   * @returns {object} - Grade display info
   */
  getGradeDisplay() {
    if (!this.gradingData) return null;

    const grade = this.gradingData.grade;
    const maxGrade = this.gradingData.maxGrade || 100;
    const percentage = (grade / maxGrade) * 100;

    let gradeClass = 'grade-low';
    if (percentage >= 90) gradeClass = 'grade-excellent';
    else if (percentage >= 80) gradeClass = 'grade-good';
    else if (percentage >= 70) gradeClass = 'grade-fair';
    else if (percentage >= 60) gradeClass = 'grade-poor';

    return {
      value: grade,
      max: maxGrade,
      percentage: percentage.toFixed(1),
      class: gradeClass,
      letter: this.getLetterGrade(percentage)
    };
  }

  /**
   * Convert percentage to letter grade
   * @param {number} percentage - Grade percentage
   * @returns {string} - Letter grade
   */
  getLetterGrade(percentage) {
    if (percentage >= 97) return 'A+';
    if (percentage >= 93) return 'A';
    if (percentage >= 90) return 'A-';
    if (percentage >= 87) return 'B+';
    if (percentage >= 83) return 'B';
    if (percentage >= 80) return 'B-';
    if (percentage >= 77) return 'C+';
    if (percentage >= 73) return 'C';
    if (percentage >= 70) return 'C-';
    if (percentage >= 67) return 'D+';
    if (percentage >= 63) return 'D';
    if (percentage >= 60) return 'D-';
    return 'F';
  }

  /**
   * Get feedback sections with premium gating
   * @returns {Array} - Feedback sections
   */
  getFeedbackSections() {
    if (!this.gradingData) return [];

    const sections = [];
    const feedback = this.gradingData.feedback || {};

    // Summary (always available)
    if (feedback.summary) {
      sections.push({
        id: 'summary',
        title: 'Summary',
        content: feedback.summary,
        isPremium: false,
        icon: 'summary'
      });
    }

    // Strengths (always available)
    if (feedback.strengths && feedback.strengths.length > 0) {
      sections.push({
        id: 'strengths',
        title: 'Strengths',
        content: Array.isArray(feedback.strengths) ? feedback.strengths.join('<br>') : feedback.strengths,
        isPremium: false,
        icon: 'strengths'
      });
    }

    // Areas for Improvement (always available)
    if (feedback.improvements && feedback.improvements.length > 0) {
      sections.push({
        id: 'improvements',
        title: 'Areas for Improvement',
        content: Array.isArray(feedback.improvements) ? feedback.improvements.join('<br>') : feedback.improvements,
        isPremium: false,
        icon: 'improvements'
      });
    }

    // Detailed Analysis (premium)
    if (feedback.detailedAnalysis) {
      sections.push({
        id: 'detailed',
        title: 'Detailed Analysis',
        content: feedback.detailedAnalysis,
        isPremium: true,
        icon: 'analysis'
      });
    }

    // Rubric Breakdown (premium)
    if (feedback.rubricBreakdown) {
      sections.push({
        id: 'rubric',
        title: 'Rubric Breakdown',
        content: this.renderRubric(feedback.rubricBreakdown),
        isPremium: true,
        icon: 'rubric'
      });
    }

    // Comparison with Class (premium)
    if (feedback.classComparison) {
      sections.push({
        id: 'comparison',
        title: 'Class Comparison',
        content: this.renderClassComparison(feedback.classComparison),
        isPremium: true,
        icon: 'comparison'
      });
    }

    // AI Suggestions (premium)
    if (feedback.aiSuggestions && feedback.aiSuggestions.length > 0) {
      sections.push({
        id: 'suggestions',
        title: 'AI Improvement Suggestions',
        content: this.renderSuggestions(feedback.aiSuggestions),
        isPremium: true,
        icon: 'suggestions'
      });
    }

    return sections;
  }

  /**
   * Render rubric breakdown
   * @param {Array} rubric - Rubric items
   * @returns {string} - HTML string
   */
  renderRubric(rubric) {
    if (!Array.isArray(rubric)) return '';

    return rubric.map(item => `
      <div class="rubric-item">
        <div class="rubric-header">
          <span class="rubric-criterion">${item.criterion || 'Criterion'}</span>
          <span class="rubric-score">${item.score}/${item.maxScore}</span>
        </div>
        <div class="rubric-bar">
          <div class="rubric-fill" style="width: ${(item.score / item.maxScore) * 100}%"></div>
        </div>
        ${item.feedback ? `<div class="rubric-feedback">${item.feedback}</div>` : ''}
      </div>
    `).join('');
  }

  /**
   * Render class comparison
   * @param {object} comparison - Comparison data
   * @returns {string} - HTML string
   */
  renderClassComparison(comparison) {
    const { average, median, percentile, distribution } = comparison;

    let distributionHtml = '';
    if (distribution) {
      distributionHtml = `
        <div class="distribution-chart">
          ${distribution.map(d => `
            <div class="distribution-bar" style="height: ${d.percentage}%" title="${d.grade}: ${d.count} students">
              ${d.grade}
            </div>
          `).join('')}
        </div>
      `;
    }

    return `
      <div class="comparison-stats">
        <div class="stat">
          <span class="stat-label">Class Average</span>
          <span class="stat-value">${average || 'N/A'}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Class Median</span>
          <span class="stat-value">${median || 'N/A'}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Your Percentile</span>
          <span class="stat-value">${percentile || 'N/A'}th</span>
        </div>
      </div>
      ${distributionHtml}
    `;
  }

  /**
   * Render AI suggestions
   * @param {Array} suggestions - AI suggestions
   * @returns {string} - HTML string
   */
  renderSuggestions(suggestions) {
    if (!Array.isArray(suggestions)) return '';

    return suggestions.map(suggestion => `
      <div class="suggestion-item">
        <div class="suggestion-title">${suggestion.title || 'Suggestion'}</div>
        <div class="suggestion-content">${suggestion.content || suggestion}</div>
        ${suggestion.example ? `<div class="suggestion-example"><strong>Example:</strong> ${suggestion.example}</div>` : ''}
        ${suggestion.priority ? `<span class="suggestion-priority priority-${suggestion.priority}">${suggestion.priority}</span>` : ''}
      </div>
    `).join('');
  }

  /**
   * Toggle section expansion
   * @param {string} sectionId - Section ID to toggle
   */
  toggleSection(sectionId) {
    // Check premium gating
    const section = this.getFeedbackSections().find(s => s.id === sectionId);
    if (section && section.isPremium && !this.isPremium) {
      this.onPremiumRequired({
        reason: 'detailed_feedback',
        section: sectionId,
        message: `${section.title} requires Premium subscription`
      });
      return;
    }

    if (this.expandedSections.has(sectionId)) {
      this.expandedSections.delete(sectionId);
    } else {
      this.expandedSections.add(sectionId);
    }

    this.render();
    this.onExpandRequested(sectionId, this.expandedSections.has(sectionId));
  }

  /**
   * Export grading results
   * @param {string} format - Export format (pdf, json, csv)
   */
  async export(format = 'pdf') {
    if (!this.isPremium && format !== 'json') {
      this.onPremiumRequired({
        reason: 'export',
        format,
        message: `${format.toUpperCase()} export requires Premium subscription`
      });
      return;
    }

    this.onExportRequested(format);

    try {
      const headers = { 'Content-Type': 'application/json' };
      if (this.csrfToken) {
        headers['X-CSRFToken'] = this.csrfToken;
      }

      const response = await fetch(`${this.apiEndpoint}/${this.gradingData.id}/export`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ format })
      });

      if (!response.ok) {
        throw new Error('Export failed');
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `grading-${this.gradingData.id}.${format}`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      console.error('GradeView: Export failed', err);
    }
  }

  /**
   * Render error state
   * @param {string} message - Error message
   */
  renderError(message) {
    if (!this.container) return;

    this.container.innerHTML = `
      <div class="grade-view grade-view-error">
        <div class="error-message">
          <span class="error-icon">!</span>
          <span>${message || 'Failed to load grading results'}</span>
        </div>
      </div>
    `;
  }

  /**
   * Render loading state
   */
  renderLoading() {
    if (!this.container) return;

    this.container.innerHTML = `
      <div class="grade-view grade-view-loading">
        <div class="loading-spinner"></div>
        <span>Loading grading results...</span>
      </div>
    `;
  }

  /**
   * Render empty state
   */
  renderEmpty() {
    if (!this.container) return;

    this.container.innerHTML = `
      <div class="grade-view grade-view-empty">
        <span>No grading results available</span>
      </div>
    `;
  }

  /**
   * Main render method
   */
  render() {
    if (!this.container) return;

    if (!this.gradingData) {
      this.renderEmpty();
      return;
    }

    const gradeDisplay = this.getGradeDisplay();
    const sections = this.getFeedbackSections();
    const feedback = this.gradingData.feedback || {};

    let html = `
      <div class="grade-view">
        <div class="grade-header">
          <div class="grade-main">
            <div class="grade-badge ${gradeDisplay.class}">
              <span class="grade-letter">${gradeDisplay.letter}</span>
              <span class="grade-value">${gradeDisplay.value}/${gradeDisplay.max}</span>
              <span class="grade-percentage">${gradeDisplay.percentage}%</span>
            </div>
            ${this.gradingData.title ? `<h3 class="grade-title">${this.gradingData.title}</h3>` : ''}
            ${this.gradingData.submittedAt ? `<span class="grade-date">Submitted: ${this.gradingData.submittedAt}</span>` : ''}
          </div>
          <div class="grade-actions">
            ${this.isPremium ? `
              <button class="btn btn-sm" onclick="gradeView.export('pdf')">Export PDF</button>
              <button class="btn btn-sm" onclick="gradeView.export('json')">Export JSON</button>
            ` : `
              <button class="btn btn-sm btn-premium" onclick="gradeView.onPremiumRequired({reason: 'export', message: 'Export requires Premium'})">
                <span class="premium-icon">★</span> Export
              </button>
            `}
          </div>
        </div>

        ${feedback.overallComment ? `
          <div class="feedback-section overall-comment">
            <h4>Overall Comment</h4>
            <p>${feedback.overallComment}</p>
          </div>
        ` : ''}

        <div class="feedback-sections">
          ${sections.map(section => {
            const isExpanded = this.expandedSections.has(section.id);
            const isLocked = section.isPremium && !this.isPremium;

            return `
              <div class="feedback-section ${isExpanded ? 'expanded' : ''} ${isLocked ? 'locked' : ''}" data-section="${section.id}">
                <div class="section-header" onclick="gradeView.toggleSection('${section.id}')">
                  <span class="section-icon">${this.getSectionIcon(section.icon)}</span>
                  <span class="section-title">${section.title}</span>
                  <span class="section-toggle">${isExpanded ? '−' : '+'}</span>
                  ${isLocked ? '<span class="lock-icon">🔒</span>' : ''}
                </div>
                ${isExpanded || !section.isPremium ? `
                  <div class="section-content">
                    ${section.content}
                  </div>
                ` : ''}
                ${isLocked && !isExpanded ? `
                  <div class="section-preview">
                    <p class="preview-text">Preview available with Premium</p>
                    <button class="btn btn-premium btn-sm" onclick="gradeView.onPremiumRequired({reason: 'section_access', section: '${section.id}'})">
                      Unlock with Premium
                    </button>
                  </div>
                ` : ''}
              </div>
            `;
          }).join('')}
        </div>

        ${!this.isPremium ? `
          <div class="premium-upsell">
            <div class="premium-badge-large">
              <span class="star">★</span>
              <span>Upgrade to Premium</span>
            </div>
            <ul class="premium-features">
              <li>Detailed Analysis & Rubric Breakdown</li>
              <li>Class Comparison & Percentile Ranking</li>
              <li>AI-Powered Improvement Suggestions</li>
              <li>Export to PDF/JSON</li>
            </ul>
          </div>
        ` : ''}
      </div>
    `;

    this.container.innerHTML = html;
  }

  /**
   * Get icon for section
   * @param {string} icon - Icon type
   * @returns {string} - Icon HTML
   */
  getSectionIcon(icon) {
    const icons = {
      summary: '📋',
      strengths: '✓',
      improvements: '↑',
      analysis: '🔬',
      rubric: '📊',
      comparison: '📈',
      suggestions: '💡'
    };
    return icons[icon] || '•';
  }

  /**
   * Update premium status
   * @param {boolean} isPremium - Premium status
   */
  setPremiumStatus(isPremium) {
    this.isPremium = isPremium;
    this.render();
  }
}

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
  module.exports = GradeView;
}

// Global instance for onclick handlers
let gradeView;
