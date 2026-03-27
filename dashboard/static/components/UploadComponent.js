/**
 * UploadComponent - File upload component with validation
 * Handles file type validation, size limits, and premium gating
 */
class UploadComponent {
  constructor(options = {}) {
    this.targetPath = options.targetPath || '';
    this.allowedTypes = options.allowedTypes || ['*'];
    this.maxFileSize = options.maxFileSize || 10 * 1024 * 1024; // 10MB default
    this.maxTotalSize = options.maxTotalSize || 50 * 1024 * 1024; // 50MB default
    this.onUploadStart = options.onUploadStart || (() => {});
    this.onUploadProgress = options.onUploadProgress || (() => {});
    this.onUploadComplete = options.onUploadComplete || (() => {});
    this.onUploadError = options.onUploadError || (() => {});
    this.onPremiumRequired = options.onPremiumRequired || (() => {});
    this.isPremium = options.isPremium || false;
    this.apiEndpoint = options.apiEndpoint || '/api/vault/upload';
    this.csrfToken = options.csrfToken || '';
  }

  /**
   * Validate file type against allowed types
   * @param {File} file - File to validate
   * @returns {object} - { valid: boolean, error: string|null }
   */
  validateFileType(file) {
    if (this.allowedTypes.includes('*')) {
      return { valid: true, error: null };
    }

    const fileType = file.type.toLowerCase();
    const fileName = file.name.toLowerCase();
    const extension = fileName.split('.').pop();

    const isAllowed = this.allowedTypes.some(type => {
      const lowerType = type.toLowerCase();
      // Check MIME type
      if (lowerType.includes('/') && fileType === lowerType) {
        return true;
      }
      // Check extension
      if (!lowerType.includes('/') && extension === lowerType.replace('.', '')) {
        return true;
      }
      return false;
    });

    if (!isAllowed) {
      return {
        valid: false,
        error: `File type "${extension}" is not allowed. Allowed types: ${this.allowedTypes.join(', ')}`
      };
    }

    return { valid: true, error: null };
  }

  /**
   * Validate file size
   * @param {File} file - File to validate
   * @returns {object} - { valid: boolean, error: string|null }
   */
  validateFileSize(file) {
    if (file.size > this.maxFileSize) {
      const maxSizeMB = (this.maxFileSize / (1024 * 1024)).toFixed(2);
      const fileSizeMB = (file.size / (1024 * 1024)).toFixed(2);
      return {
        valid: false,
        error: `File "${file.name}" (${fileSizeMB}MB) exceeds maximum size of ${maxSizeMB}MB`
      };
    }
    return { valid: true, error: null };
  }

  /**
   * Validate total size of multiple files
   * @param {FileList|File[]} files - Files to validate
   * @returns {object} - { valid: boolean, error: string|null }
   */
  validateTotalSize(files) {
    let totalSize = 0;
    for (const file of files) {
      totalSize += file.size;
    }

    if (totalSize > this.maxTotalSize) {
      const maxTotalMB = (this.maxTotalSize / (1024 * 1024)).toFixed(2);
      const totalMB = (totalSize / (1024 * 1024)).toFixed(2);
      return {
        valid: false,
        error: `Total size (${totalMB}MB) exceeds maximum allowed (${maxTotalMB}MB)`
      };
    }
    return { valid: true, error: null };
  }

  /**
   * Validate all files before upload
   * @param {FileList|File[]} files - Files to validate
   * @returns {object} - { valid: boolean, errors: string[], validFiles: File[] }
   */
  validateFiles(files) {
    const errors = [];
    const validFiles = [];

    // Check total size first
    const totalSizeResult = this.validateTotalSize(files);
    if (!totalSizeResult.valid) {
      // Check if this is a premium feature
      if (!this.isPremium && files.length > 5) {
        this.onPremiumRequired({
          reason: 'bulk_upload',
          message: 'Bulk upload (more than 5 files) requires Premium subscription',
          currentCount: files.length,
          freeLimit: 5
        });
        return { valid: false, errors: ['Premium required for bulk upload'], validFiles: [] };
      }
      errors.push(totalSizeResult.error);
    }

    // Validate each file
    for (const file of files) {
      const typeResult = this.validateFileType(file);
      const sizeResult = this.validateFileSize(file);

      if (!typeResult.valid) {
        errors.push(typeResult.error);
      } else if (!sizeResult.valid) {
        errors.push(sizeResult.error);
      } else {
        validFiles.push(file);
      }
    }

    return {
      valid: errors.length === 0,
      errors,
      validFiles
    };
  }

  /**
   * Upload files to the server
   * @param {FileList|File[]} files - Files to upload
   * @param {object} additionalData - Additional form data
   * @returns {Promise<object>} - Upload result
   */
  async upload(files, additionalData = {}) {
    const validation = this.validateFiles(files);

    if (!validation.valid) {
      this.onUploadError({
        type: 'validation',
        errors: validation.errors
      });
      return { success: false, errors: validation.errors };
    }

    if (validation.validFiles.length === 0) {
      return { success: false, errors: ['No valid files to upload'] };
    }

    this.onUploadStart({ fileCount: validation.validFiles.length });

    const formData = new FormData();

    // Add files
    for (const file of validation.validFiles) {
      formData.append('files', file);
    }

    // Add additional data
    if (additionalData.targetPath) {
      formData.append('target_path', additionalData.targetPath);
    }
    if (additionalData.namespace) {
      formData.append('namespace', additionalData.namespace);
    }
    if (additionalData.relativePaths) {
      formData.append('relative_paths', additionalData.relativePaths);
    }

    // Add CSRF token
    const headers = {};
    if (this.csrfToken) {
      headers['X-CSRFToken'] = this.csrfToken;
    }

    try {
      const response = await fetch(this.apiEndpoint, {
        method: 'POST',
        headers,
        body: formData
      });

      const result = await response.json().catch(() => ({}));

      if (response.ok) {
        this.onUploadComplete(result);
        return { success: true, result };
      } else {
        const error = result.error || 'Upload failed';
        this.onUploadError({
          type: 'server',
          error,
          status: response.status
        });
        return { success: false, error, status: response.status };
      }
    } catch (err) {
      this.onUploadError({
        type: 'network',
        error: err.message
      });
      return { success: false, error: err.message };
    }
  }

  /**
   * Create and render file input element
   * @param {HTMLElement} container - Container element
   * @returns {HTMLInputElement} - File input element
   */
  renderFileInput(container) {
    const input = document.createElement('input');
    input.type = 'file';
    input.className = 'form-control';
    input.multiple = true;

    if (this.allowedTypes.length > 0 && !this.allowedTypes.includes('*')) {
      input.accept = this.allowedTypes.map(t => t.includes('/') ? t : `.${t.replace('.', '')}`).join(',');
    }

    input.addEventListener('change', (e) => {
      const files = Array.from(e.target.files);
      const validation = this.validateFiles(files);

      // Update UI with validation status
      const statusEl = container.querySelector('.upload-status');
      if (statusEl) {
        if (validation.valid) {
          statusEl.className = 'upload-status upload-status-success';
          statusEl.textContent = `${files.length} file(s) ready to upload`;
        } else {
          statusEl.className = 'upload-status upload-status-error';
          statusEl.textContent = validation.errors.join('; ');
        }
      }
    });

    container.appendChild(input);
    return input;
  }

  /**
   * Render upload button
   * @param {HTMLElement} container - Container element
   * @param {string} label - Button label
   * @returns {HTMLButtonElement} - Button element
   */
  renderUploadButton(container, label = 'Upload') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-primary';
    button.textContent = label;

    button.addEventListener('click', async () => {
      const input = container.querySelector('input[type="file"]');
      if (input && input.files.length > 0) {
        await this.upload(input.files);
      }
    });

    container.appendChild(button);
    return button;
  }

  /**
   * Render full upload component
   * @param {HTMLElement} container - Container element
   * @param {object} options - Render options
   */
  render(container, options = {}) {
    const {
      showFileInput = true,
      showButton = true,
      buttonLabel = 'Upload',
      showStatus = true
    } = options;

    container.className = 'upload-component';

    if (showStatus) {
      const statusEl = document.createElement('div');
      statusEl.className = 'upload-status';
      container.appendChild(statusEl);
    }

    if (showFileInput) {
      this.renderFileInput(container);
    }

    if (showButton) {
      this.renderUploadButton(container, buttonLabel);
    }

    // Add premium indicator if not premium
    if (!this.isPremium) {
      const premiumEl = document.createElement('div');
      premiumEl.className = 'premium-indicator';
      premiumEl.innerHTML = `
        <span class="premium-badge">Free Tier</span>
        <small>Upgrade to Premium for bulk upload and larger files</small>
      `;
      container.appendChild(premiumEl);
    }
  }

  /**
   * Update premium status
   * @param {boolean} isPremium - Premium status
   */
  setPremiumStatus(isPremium) {
    this.isPremium = isPremium;

    // Update UI if rendered
    const container = document.querySelector('.upload-component');
    if (container) {
      const premiumEl = container.querySelector('.premium-indicator');
      if (premiumEl) {
        premiumEl.remove();
      }
      if (!isPremium) {
        const newPremiumEl = document.createElement('div');
        newPremiumEl.className = 'premium-indicator';
        newPremiumEl.innerHTML = `
          <span class="premium-badge">Free Tier</span>
          <small>Upgrade to Premium for bulk upload and larger files</small>
        `;
        container.appendChild(newPremiumEl);
      }
    }
  }
}

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
  module.exports = UploadComponent;
}
